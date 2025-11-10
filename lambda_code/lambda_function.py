# ===============================================================
# Lambda: CloudWatch Metrics → Excel (Images-only) → S3 + Email
# - Multi-region, single account
# - Email: short body, attach ZIP only (no S3 links)
# ===============================================================

import os, io, re, time, json, boto3, zipfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from botocore.exceptions import ClientError, BotoCoreError, PaginationError
import xlsxwriter

# ---------------------------
# Sessions & Clients
# ---------------------------
SESSION = boto3.session.Session()
STS = SESSION.client("sts")
S3  = SESSION.client("s3")
SES = SESSION.client("ses")

def cw_client(region: str):
    return SESSION.client("cloudwatch", region_name=region)

# ---------------------------
# Environment
# ---------------------------
SES_SENDER_EMAIL     = os.environ["SES_SENDER_EMAIL"]
SES_RECIPIENT_EMAILS = [x.strip() for x in os.environ["SES_RECIPIENT_EMAILS"].split(",") if x.strip()]
S3_BUCKET            = os.environ["S3_BUCKET"]

S3_PREFIX_BASE      = os.getenv("S3_PREFIX_BASE", "cloudwatch/excel")
REGIONS             = [r.strip() for r in os.getenv("REGIONS", "us-east-1,ap-northeast-1,ap-southeast-1").split(",") if r.strip()]
NAMESPACES          = [x.strip() for x in os.getenv("NAMESPACES", "").split(",") if x.strip()]
LOOKBACK_ISO        = os.getenv("LOOKBACK_ISO", "-PT24H")
PERIOD_SECONDS      = int(os.getenv("PERIOD_SECONDS", "300"))
MAX_METRICS_PER_NS  = int(os.getenv("MAX_METRICS_PER_NS", "60"))
CONCURRENCY         = int(os.getenv("CONCURRENCY", "12"))
IMG_SCALE           = float(os.getenv("IMG_SCALE", "0.35"))
WIDGET_WIDTH        = int(os.getenv("WIDGET_WIDTH", "1067"))
WIDGET_HEIGHT       = int(os.getenv("WIDGET_HEIGHT", "300"))
RENDER_SLEEP_SEC    = float(os.getenv("RENDER_SLEEP_SEC", "0.0"))

# Excel label formatting
METRIC_LABEL_FONT_SIZE = int(os.getenv("METRIC_LABEL_FONT_SIZE", "11"))
METRIC_LABEL_BOLD      = os.getenv("METRIC_LABEL_BOLD", "true").lower() in ("1", "true", "yes")

# ---------------------------
# Helpers
# ---------------------------
def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)

def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def get_account_id() -> str:
    return STS.get_caller_identity()["Account"]

def paginate_list_metrics(cw, **kwargs):
    try:
        paginator = cw.get_paginator("list_metrics")
        for page in paginator.paginate(**kwargs):
            yield page
    except PaginationError as e:
        print(f"[WARN] Paginator fallback: {e}")
        token = None
        while True:
            params = dict(kwargs)
            if token:
                params["NextToken"] = token
            resp = cw.list_metrics(**params)
            yield resp
            token = resp.get("NextToken")
            if not token:
                break

def list_namespaces(cw) -> list[str]:
    namespaces = set()
    for page in paginate_list_metrics(cw):
        for m in page.get("Metrics", []):
            ns = m.get("Namespace")
            if ns:
                namespaces.add(ns)
        if len(namespaces) > 2000:
            break
    return sorted(namespaces)

def list_metrics_in_namespace(cw, ns: str) -> list[dict]:
    out = []
    for page in paginate_list_metrics(cw, Namespace=ns):
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

def render_widget_image(cw, widget: dict, max_retries: int = 3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            if RENDER_SLEEP_SEC:
                time.sleep(RENDER_SLEEP_SEC)
            resp = cw.get_metric_widget_image(MetricWidget=json.dumps(widget))
            return resp["MetricWidgetImage"]
        except (ClientError, BotoCoreError) as e:
            last_err = e
            time.sleep(0.3 * attempt)
    print(f"[WARN] Failed widget '{widget.get('title')}': {last_err}")
    return None

# ---------------------------
# Excel (Images-only)
# ---------------------------
def build_excel_images_only(namespace: str, region: str, items: list[dict], scanned_count: int) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    title_fmt   = wb.add_format({"bold": True, "font_size": 18})
    sub_fmt     = wb.add_format({"font_size": 10, "italic": True, "font_color": "#555"})
    tile_hdr    = wb.add_format({"bold": True, "align": "center", "valign": "vcenter", "border": 1, "bg_color": "#e8f1f8"})
    tile_val    = wb.add_format({"bold": True, "font_size": 16, "align": "center", "valign": "vcenter", "border": 1, "bg_color": "#e8f1f8"})
    section_hdr = wb.add_format({"bold": True, "font_color": "#2b4c7e", "bg_color": "#dfe8f7", "border": 1})
    label_fmt   = wb.add_format({"font_size": METRIC_LABEL_FONT_SIZE, "bold": METRIC_LABEL_BOLD})

    ws = wb.add_worksheet("Dashboard")
    ws.hide_gridlines(2)
    ws.set_column(0, 7, 32)
    ws.set_row(0, 28); ws.set_row(1, 18)

    ws.write("A1", f"{namespace} — CloudWatch Dashboard", title_fmt)
    ws.write("A2", f"Region: {region} | Lookback: {LOOKBACK_ISO} | Period: {PERIOD_SECONDS}s | Generated: {iso_now()}", sub_fmt)

    ws.merge_range("A4:C4", "Charts rendered", tile_hdr)
    ws.merge_range("A5:C6", str(sum(1 for it in items if it.get('img'))), tile_val)

    ws.merge_range("D4:F4", "Metrics scanned", tile_hdr)
    ws.merge_range("D5:F6", str(scanned_count), tile_val)

    ws.merge_range("A8:F8", "Charts", section_hdr)

    start_row   = 9
    col_count   = 2
    row_stride  = 24
    col_stride  = 3
    label_offset= 20
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
        ws.write(r + label_offset, c, it["title"], label_fmt)

    wb.close()
    buf.seek(0)
    return buf.read()

# ---------------------------
# S3 + Email helpers
# ---------------------------
def s3_put(key: str, data: bytes, content_type: str) -> str:
    S3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{S3_BUCKET}/{key}"

def human_period(lookback_iso: str) -> str:
    iso = lookback_iso.upper()
    if iso in ("-PT24H", "-P1D"):
        return "the past day"
    if iso in ("-P7D", "-P1W"):
        return "the past week"
    return f"lookback {lookback_iso}"

def send_email_zip_only(summary_lines: list[str], zip_bytes: bytes, zip_filename: str = "dashboards.zip"):
    """
    Sends short email with ZIP attached (if <= 9.5 MB). No S3 links.
    """
    msg = MIMEMultipart()
    msg["Subject"] = "CloudWatch Metric Dashboards"
    msg["From"] = SES_SENDER_EMAIL
    msg["To"] = ", ".join(SES_RECIPIENT_EMAILS)

    body_lines = [
        "Hi team,",
        "",
        f"Please find attached the CloudWatch metric dashboards for {human_period(LOOKBACK_ISO)}.",
        "",
        *summary_lines,
        "",
        "Best regards,",
        "AWS CloudWatch Dashboard Automation",
        "",
        "— Automated Report via AWS Lambda"
    ]
    msg.attach(MIMEText("\n".join(body_lines), "plain"))

    if not zip_bytes:
        print("[WARN] No ZIP bytes found — sending without attachment.")
    else:
        raw_size = len(zip_bytes)
        limit = 9.5 * 1024 * 1024
        print(f"[INFO] ZIP size: {raw_size/1024/1024:.2f} MB")
        if raw_size <= limit:
            part = MIMEApplication(zip_bytes)
            part.add_header("Content-Disposition", "attachment", filename=zip_filename)
            msg.attach(part)
            print(f"[INFO] ZIP attached: {zip_filename}")
        else:
            print(f"[WARN] ZIP too large (>9.5 MB) — sending without attachment.")

    resp = SES.send_raw_email(
        Source=SES_SENDER_EMAIL,
        Destinations=SES_RECIPIENT_EMAILS,
        RawMessage={"Data": msg.as_string()},
    )
    print(f"[INFO] SES MessageId: {resp.get('MessageId')}")

# ---------------------------
# Lambda handler
# ---------------------------
def lambda_handler(event, context):
    account_id = get_account_id()
    ts_folder = iso_now()
    print(f"[INFO] Start | account={account_id} | regions={REGIONS}")

    excel_index = {}
    total_rendered = 0

    for region in REGIONS:
        cw = cw_client(region)
        target_namespaces = NAMESPACES if NAMESPACES else list_namespaces(cw)
        print(f"[INFO] Region {region}: {len(target_namespaces)} namespaces")

        for ns in target_namespaces:
            metrics = list_metrics_in_namespace(cw, ns)
            print(f"[INFO] {region} | {ns}: {len(metrics)} metrics (cap {MAX_METRICS_PER_NS})")
            if not metrics:
                continue

            widgets = [build_widget(m) for m in metrics]
            rendered_items = []
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
                fut_to_idx = {ex.submit(render_widget_image, cw, w): i for i, w in enumerate(widgets)}
                for fut in as_completed(fut_to_idx):
                    i = fut_to_idx[fut]
                    w = widgets[i]
                    m = metrics[i]
                    try:
                        img = fut.result()
                    except Exception as e:
                        print(f"[WARN] Render error {region}/{ns}/{w.get('title')}: {e}")
                        img = None
                    rendered_items.append({"title": w["title"], "img": img, "metric": m})

            rendered_items.sort(key=lambda it: it["title"])
            charts_rendered = sum(1 for it in rendered_items if it["img"])
            if charts_rendered == 0:
                print(f"[INFO] {region} | {ns}: no charts rendered — skipping Excel.")
                continue

            total_rendered += charts_rendered
            excel_bytes = build_excel_images_only(ns, region, rendered_items, len(metrics))

            key = f"{S3_PREFIX_BASE}/{account_id}/{region}/{safe(ns)}/{ts_folder}/{safe(ns)}.xlsx"
            s3_put(key, excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            excel_index.setdefault(region, {})
            excel_index[region][ns] = {"bytes": excel_bytes, "count": charts_rendered}

    if not excel_index:
        return {"status": "no_excels", "account": account_id}

    # --- Build ZIP for all regions/namespaces
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
        for region, ns_map in excel_index.items():
            for ns, info in ns_map.items():
                z.writestr(f"{region}/{safe(ns)}.xlsx", info["bytes"])
    zip_buf.seek(0)
    zip_bytes = zip_buf.read()

    # Upload ZIP for durability (not linked in email)
    zip_key = f"{S3_PREFIX_BASE}/{account_id}/{ts_folder}/dashboards.zip"
    s3_put(zip_key, zip_bytes, "application/zip")

    # --- Email summary
    lines = [f"Account: {account_id}", f"Run timestamp: {ts_folder}", ""]
    for region in sorted(excel_index.keys()):
        ns_count = len(excel_index[region])
        chart_count = sum(info["count"] for info in excel_index[region].values())
        lines.append(f"{region}: {ns_count} namespaces, {chart_count} charts")

    send_email_zip_only(lines, zip_bytes, "cloudwatch_dashboards.zip")

    return {
        "status": "email_sent",
        "account": account_id,
        "regions": list(excel_index.keys()),
        "total_metrics_rendered": total_rendered,
        "zip_s3_key": zip_key
    }
