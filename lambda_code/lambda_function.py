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
    """
    Safe paginator for CloudWatch ListMetrics.
    ListMetrics does NOT support PaginationConfig/PageSize.
    """
    try:
        paginator = CW.get_paginator("list_metrics")
        # No PaginationConfig here
        for page in paginator.paginate(**kwargs):
            yield page
    except PaginationError as e:
        # Fallback to manual NextToken loop if paginator misbehaves
        print(f"[WARN] Paginator error, falling back to manual loop: {e}")
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
    dims = metric.get("Dimensions", [])
    dim_pairs = []
    for d in dims:
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

def build_excel(namespace: str, image_map: list[tuple[str, bytes]]) -> bytes:
    """
    Excel sheet with 2-column grid; each tile ~20 rows; labels under images.
    """
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Dashboard")

    title_fmt = wb.add_format({"bold": True, "font_size": 14})
    meta_fmt  = wb.add_format({"font_size": 10, "italic": True})
    ws.write("A1", f"CloudWatch Metrics: {namespace}", title_fmt)
    ws.write("A2", f"Region: {REGION} | Lookback: {LOOKBACK_ISO} | Period: {PERIOD_SECONDS}s | Generated: {iso_now()}", meta_fmt)
    ws.set_column(0, 3, 40)

    row_base   = 4
    col_count  = 2
    row_stride = 20
    col_stride = 2  # A/C columns

    for idx, (title, img_data) in enumerate(image_map):
        if not img_data:
            continue
        r = row_base + (idx // col_count) * row_stride
        c = (idx % col_count) * col_stride
        ws.insert_image(r, c, f"{safe(title)}.png",
                        {"image_data": io.BytesIO(img_data),
                         "x_scale": IMG_SCALE, "y_scale": IMG_SCALE})
        ws.write(r + 17, c, title)

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
            # base64 expansion estimate (~4/3) + small MIME overhead
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

        image_map = []
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            fut_to_w = {ex.submit(render_widget_image, w): w for w in widgets}
            for fut in as_completed(fut_to_w):
                w = fut_to_w[fut]
                img = None
                try:
                    img = fut.result()
                except Exception as e:
                    print(f"[WARN] Exception rendering '{w.get('title')}': {e}")
                if img:
                    image_map.append((w["title"], img))

        if not image_map:
            print(f"[INFO] {ns}: no images rendered, skipping Excel")
            continue

        total_rendered += len(image_map)
        excel_bytes = build_excel(ns, image_map)

        key = f"{S3_PREFIX}/{ts_folder}/{safe(ns)}.xlsx"
        s3_path = s3_put(key, excel_bytes,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        ns_to_excel[ns] = {
            "bytes": excel_bytes,
            "s3_path": s3_path,
            "size_mb": len(excel_bytes) / (1024 * 1024),
            "count": len(image_map),
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
