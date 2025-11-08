# ===============================================================
# Lambda: CloudWatch Metrics → Excel Dashboards → S3 + Email
# ===============================================================
import os, io, json, re, time, math, boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from botocore.exceptions import ClientError, BotoCoreError, PaginationError
import xlsxwriter
from datetime import datetime, timezone

# ---------------------------
# AWS clients/session
# ---------------------------
SESSION = boto3.session.Session()
REGION  = SESSION.region_name
CW  = SESSION.client("cloudwatch")
S3  = SESSION.client("s3")
SES = SESSION.client("ses")

# ---------------------------
# Environment variables
# ---------------------------
SES_SENDER_EMAIL     = os.environ["SES_SENDER_EMAIL"]
SES_RECIPIENT_EMAILS = [x.strip() for x in os.environ["SES_RECIPIENT_EMAILS"].split(",")]
S3_BUCKET            = os.environ["S3_BUCKET"]

# Optional tuning
NAMESPACES         = [x.strip() for x in os.getenv("NAMESPACES", "").split(",") if x.strip()]
LOOKBACK_ISO       = os.getenv("LOOKBACK_ISO", "-PT24H")       # 24h lookback
PERIOD_SECONDS     = int(os.getenv("PERIOD_SECONDS", "300"))   # 5m
MAX_METRICS_PER_NS = int(os.getenv("MAX_METRICS_PER_NS", "100"))
IMG_SCALE          = float(os.getenv("IMG_SCALE", "0.35"))
ATTACH_EXCEL       = os.getenv("ATTACH_EXCEL", "true").lower() == "true"
MAX_EMAIL_MB       = float(os.getenv("MAX_EMAIL_MB", "7"))
CONCURRENCY        = int(os.getenv("CONCURRENCY", "8"))
WIDGET_WIDTH       = int(os.getenv("WIDGET_WIDTH", "1067"))
WIDGET_HEIGHT      = int(os.getenv("WIDGET_HEIGHT", "300"))
S3_PREFIX          = os.getenv("S3_PREFIX", "cloudwatch/excel")
RENDER_SLEEP_SEC   = float(os.getenv("RENDER_SLEEP_SEC", "0.0"))

# ---------------------------
# Helpers
# ---------------------------
def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)

def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def paginate_list_metrics(**kwargs):
    """Safe paginator for CloudWatch ListMetrics (no PageSize)."""
    try:
        paginator = CW.get_paginator("list_metrics")
        for page in paginator.paginate(**kwargs):
            yield page
    except PaginationError as e:
        print(f"[WARN] Paginator error, fallback to manual NextToken: {e}")
        token = None
        while True:
            params = dict(kwargs)
            if token:
                params["NextToken"] = token
            resp = CW.list_metrics(**params)
            yield resp
            token = resp.get("NextToken")
            if not token:
                break

def list_namespaces() -> list[str]:
    namespaces = set()
    for page in paginate_list_metrics():
        for m in page.get("Metrics", []):
            ns = m.get("Namespace")
            if ns:
                namespaces.add(ns)
        if len(namespaces) > 2000:
            break
    return sorted(namespaces)

def list_metrics_in_namespace(ns: str) -> list[dict]:
    out = []
    for page in paginate_list_metrics(Namespace=ns):
        out.extend(page.get("Metrics", []))
        if len(out) >= MAX_METRICS_PER_NS:
            break
    return out[:MAX_METRICS_PER_NS]

def build_widget(metric: dict) -> dict:
    ns, name = metric["Namespace"], metric["MetricName"]
    dim_pairs = []
    for d in metric.get("Dimensions", []):
        dim_pairs += [d["Name"], d["Value"]]
    return {
        "title": name,
        "view": "timeSeries",
        "stacked": False,
        "stat": "Average",
        "period": PERIOD_SECONDS,
        "metrics": [[ns, name] + dim_pairs],
        "start": LOOKBACK_ISO,
        "end": "PT0M",
        "width": WIDGET_WIDTH,
        "height": WIDGET_HEIGHT,
    }

def render_widget_image(widget: dict, max_retries: int = 3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            if RENDER_SLEEP_SEC:
                time.sleep(RENDER_SLEEP_SEC)
            resp = CW.get_metric_widget_image(MetricWidget=json.dumps(widget))
            return resp["MetricWidgetImage"]
        except (ClientError, BotoCoreError) as e:
            last_err = e
            time.sleep(0.3 * attempt)
    print(f"[WARN] Failed to render '{widget.get('title')}' after retries: {last_err}")
    return None

# ---------- Excel builder: KPI tiles + image grid + catalog ----------
def format_dims(metric: dict) -> str:
    dims = metric.get("Dimensions", [])
    if not dims:
        return "-"
    return ", ".join(f"{d['Name']}={d['Value']}" for d in dims)

def build_excel(namespace: str, items: list[dict], scanned_count: int) -> bytes:
    """
    Build a polished dashboard workbook.
    items: list of {"title": str, "img": bytes, "metric": dict}
    scanned_count: how many metrics were scanned in the namespace (pre-render)
    """
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    # ---------- Formats ----------
    title_fmt   = wb.add_format({"bold": True, "font_size": 18})
    sub_fmt     = wb.add_format({"font_size": 10, "italic": True, "font_color": "#555555"})
    tile_hdr    = wb.add_format({"bold": True, "font_color": "#3f4751", "align": "center", "valign": "vcenter"})
    tile_val    = wb.add_format({"bold": True, "font_size": 16, "align": "center", "valign": "vcenter"})
    tile_box    = wb.add_format({"border": 1, "bg_color": "#e8f1f8"})
    section_hdr = wb.add_format({"bold": True, "font_color": "#2b4c7e", "bg_color": "#dfe8f7", "border": 1})
    small       = wb.add_format({"font_size": 9})
    link_fmt    = wb.add_format({"font_color": "blue", "underline": 1})

    # ---------- Dashboard sheet ----------
    ws = wb.add_worksheet("Dashboard")
    ws.set_column(0, 7, 32)         # generous width for tiles/labels
    ws.set_row(0, 28); ws.set_row(1, 18)

    ws.write("A1", f"{namespace} — CloudWatch KPI Dashboard", title_fmt)
    ws.write("A2", f"Region: {REGION} | Lookback: {LOOKBACK_ISO} | Period: {PERIOD_SECONDS}s | Generated: {iso_now()}", sub_fmt)

    # KPI tiles (merge ranges)
    #   A4:C6 | D4:F6 | A8:C10 | D8:F10
    ws.merge_range("A4:C4", "Charts rendered", tile_hdr)
    ws.merge_range("A5:C6", f"{sum(1 for it in items if it.get('img'))}", wb.add_format({**tile_box.properties, **tile_val.properties}))

    ws.merge_range("D4:F4", "Metrics scanned", tile_hdr)
    ws.merge_range("D5:F6", f"{scanned_count}", wb.add_format({**tile_box.properties, **tile_val.properties}))

    ws.merge_range("A8:C8", "Lookback", tile_hdr)
    ws.merge_range("A9:C10", LOOKBACK_ISO, wb.add_format({**tile_box.properties, **tile_val.properties}))

    ws.merge_range("D8:F8", "Period (seconds)", tile_hdr)
    ws.merge_range("D9:F10", f"{PERIOD_SECONDS}", wb.add_format({**tile_box.properties, **tile_val.properties}))

    # Section header for charts
    ws.merge_range("A12:F12", "Charts", section_hdr)

    # Image grid (2 columns, generous spacing)
    start_row   = 13
    col_count   = 2
    row_stride  = 24     # space between tiles
    col_stride  = 3      # A/D columns effectively (0 and 3)
    label_offset= 20     # row below image
    col0        = 0

    for idx, it in enumerate(items):
        img = it.get("img")
        if not img:
            continue
        r = start_row + (idx // col_count) * row_stride
        c = col0 + (idx % col_count) * col_stride
        ws.insert_image(r, c, f"{safe(it['title'])}.png",
                        {"image_data": io.BytesIO(img),
                         "x_scale": IMG_SCALE, "y_scale": IMG_SCALE})
        # label under the chart
        ws.write(r + label_offset, c, it["title"], small)

    # Quick link to Catalog
    ws.write_url("A3", "internal:'Catalog'!A1", link_fmt, "Open Catalog →")

    # ---------- Catalog sheet ----------
    cat = wb.add_worksheet("Catalog")
    cat.set_column(0, 0, 36)   # MetricName
    cat.set_column(1, 1, 50)   # Dimensions
    cat.set_column(2, 4, 18)   # Stat/Period/Rendered

    cat.write_row(0, 0, ["Metric name", "Dimensions", "Stat", "Period (s)", "Rendered"])

    data_rows = []
    for it in items:
        m = it["metric"]
        data_rows.append([
            m["MetricName"],
            format_dims(m),
            "Average",
            PERIOD_SECONDS,
            "Yes" if it.get("img") else "No"
        ])

    # add_table gives filtering/sorting out-of-the-box
    cat.add_table(0, 0, len(data_rows), 4, {
        "data": data_rows,
        "style": "Table Style Medium 2",
        "columns": [
            {"header": "Metric name"},
            {"header": "Dimensions"},
            {"header": "Stat"},
            {"header": "Period (s)"},
            {"header": "Rendered"},
        ],
        "autofilter": True
    })

    # ---------- Readme sheet ----------
    readme = wb.add_worksheet("Readme")
    readme.set_column(0, 0, 110)
    info = [
        "About",
        f"- Namespace: {namespace}",
        f"- Region: {REGION}",
        f"- Generated: {iso_now()}",
        "",
        "How to use",
        "1) Dashboard sheet: KPI tiles and chart images.",
        "2) Catalog sheet: sortable list of all metrics scanned for this namespace.",
        "3) Images are rendered by CloudWatch GetMetricWidgetImage with 'Average' stats.",
        "",
        "Tips",
        "- To reduce file size, cap MAX_METRICS_PER_NS or decrease IMG_SCALE.",
        "- To focus a set of namespaces, set env var NAMESPACES to a comma-separated list.",
    ]
    for i, line in enumerate(info):
        readme.write(i, 0, line)

    wb.close()
    buf.seek(0)
    return buf.read()

def s3_put(key: str, data: bytes, content_type: str) -> str:
    S3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{S3_BUCKET}/{key}"

def send_email(ns_to_excel: dict, summary_lines: list[str]):
    msg = MIMEMultipart()
    msg["Subject"] = "CloudWatch Metrics Excel Dashboards"
    msg["From"] = SES_SENDER_EMAIL
    msg["To"] = ", ".join(SES_RECIPIENT_EMAILS)

    body = ["CloudWatch Excel Dashboards have been generated.", ""]
    body.extend(summary_lines)
    msg.attach(MIMEText("\n".join(body), "plain"))

    if ATTACH_EXCEL:
        total_b64_bytes = 0
        limit_bytes = int(MAX_EMAIL_MB * 1024 * 1024)
        for ns, info in ns_to_excel.items():
            b64_size = ((len(info["bytes"]) + 2) // 3) * 4 + 2048
            if total_b64_bytes + b64_size > limit_bytes:
                print(f"[INFO] Skipping attachment for {ns} (would exceed ~{MAX_EMAIL_MB} MB)")
                continue
            part = MIMEApplication(info["bytes"])
            part.add_header("Content-Disposition", "attachment", filename=f"{safe(ns)}.xlsx")
            msg.attach(part)
            total_b64_bytes += b64_size

    SES.send_raw_email(
        Source=SES_SENDER_EMAIL,
        Destinations=SES_RECIPIENT_EMAILS,
        RawMessage={"Data": msg.as_string()}
    )

# ---------------------------
# Lambda handler
# ---------------------------
def lambda_handler(event, context):
    print(f"[INFO] Start Lambda in region {REGION}")
    t0 = time.time()

    namespaces = NAMESPACES if NAMESPACES else list_namespaces()
    print(f"[INFO] Target namespaces: {len(namespaces)}")
    if not namespaces:
        return {"status": "no_namespaces"}

    ns_to_excel, total_rendered = {}, 0
    ts_folder = iso_now()

    for ns in namespaces:
        metrics = list_metrics_in_namespace(ns)
        print(f"[INFO] {ns}: {len(metrics)} metrics (cap {MAX_METRICS_PER_NS})")
        if not metrics:
            continue

        widgets = [build_widget(m) for m in metrics]

        # render images in parallel
        rendered_items = []
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            fut_to_idx = {ex.submit(render_widget_image, w): i for i, w in enumerate(widgets)}
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                w = widgets[i]
                m = metrics[i]
                img = None
                try:
                    img = fut.result()
                except Exception as e:
                    print(f"[WARN] Exception rendering '{w.get('title')}': {e}")
                rendered_items.append({"title": w["title"], "img": img, "metric": m})

        rendered_items.sort(key=lambda it: it["title"])  # stable layout
        charts_rendered = sum(1 for it in rendered_items if it["img"])
        if charts_rendered == 0:
            print(f"[INFO] {ns}: no images rendered, skipping Excel")
            continue

        total_rendered += charts_rendered
        excel_bytes = build_excel(ns, rendered_items, scanned_count=len(metrics))

        key = f"{S3_PREFIX}/{ts_folder}/{safe(ns)}.xlsx"
        s3_path = s3_put(key, excel_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        ns_to_excel[ns] = {
            "bytes": excel_bytes,
            "s3_path": s3_path,
            "size_mb": len(excel_bytes) / (1024 * 1024),
            "count": charts_rendered,
        }

    if not ns_to_excel:
        return {"status": "no_excel_files_generated"}

    # Email summary lines
    lines = []
    for ns, info in sorted(ns_to_excel.items()):
        lines.append(f"- {ns}: {info['count']} charts → {info['s3_path']} ({info['size_mb']:.2f} MB)")
    if not ATTACH_EXCEL:
        lines.append("")
        lines.append("Attachments disabled (ATTACH_EXCEL=false); see S3 links above.")

    send_email(ns_to_excel, lines)

    dt = time.time() - t0
    return {
        "status": "email_sent",
        "region": REGION,
        "bucket": S3_BUCKET,
        "s3_prefix": f"{S3_PREFIX}/{ts_folder}/",
        "namespaces": sorted(ns_to_excel.keys()),
        "per_namespace_counts": {k: v["count"] for k, v in ns_to_excel.items()},
        "total_metrics_rendered": total_rendered,
        "elapsed_sec": round(dt, 2),
        "attach_excel": ATTACH_EXCEL
    }
