# ===============================================================
# Lambda: CloudWatch Metrics -> Excel per Namespace -> S3 + Email
# (Images in memory only; upload only Excel files)
# ===============================================================
import os, io, json, re, boto3, traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from botocore.exceptions import ClientError, BotoCoreError

# ---------- AWS clients ----------
CW  = boto3.client("cloudwatch")
S3  = boto3.client("s3")
SES = boto3.client("ses")

# ---------- Config via env ----------
SES_SENDER_EMAIL      = os.environ["SES_SENDER_EMAIL"]                       # verified sender
SES_RECIPIENT_EMAILS  = [x.strip() for x in os.environ["SES_RECIPIENT_EMAILS"].split(",")]
S3_BUCKET             = os.environ["S3_BUCKET"]                              # target bucket

NAMESPACES            = [x.strip() for x in os.getenv("NAMESPACES","").split(",") if x.strip()]  # optional filter
LOOKBACK_ISO          = os.getenv("LOOKBACK_ISO","-PT24H")                   # e.g. -PT24H, -PT72H, -PT168H
PERIOD_SECONDS        = int(os.getenv("PERIOD_SECONDS","300"))               # e.g. 60, 300
MAX_METRICS_PER_NS    = int(os.getenv("MAX_METRICS_PER_NS","200"))           # safety cap per namespace
IMAGES_PER_ROW        = int(os.getenv("IMAGES_PER_ROW","2"))                 # excel layout
IMG_SCALE             = float(os.getenv("IMG_SCALE","0.35"))                 # excel image scale
ATTACH_EXCEL          = os.getenv("ATTACH_EXCEL","false").lower() == "true"  # attach small files?
MAX_EMAIL_MB          = float(os.getenv("MAX_EMAIL_MB","8"))                 # safe total attach size

WIDGET_WIDTH          = int(os.getenv("WIDGET_WIDTH","1067"))
WIDGET_HEIGHT         = int(os.getenv("WIDGET_HEIGHT","300"))

# ---------- Helpers ----------
def safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)

def s3_put(key: str, data: bytes, content_type: str):
    S3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return key

def log_info(msg):  print(f"[INFO] {msg}")
def log_warn(msg):  print(f"[WARN] {msg}")
def log_err(msg):   print(f"[ERROR] {msg}")

# ---------- CloudWatch ----------
def list_namespaces():
    """Auto-discover namespaces present in the account/region."""
    nss, token = set(), None
    tries = 0
    while True:
        tries += 1
        if tries > 100: break
        resp = CW.list_metrics(NextToken=token) if token else CW.list_metrics()
        for m in resp.get("Metrics", []):
            ns = m.get("Namespace")
            if ns: nss.add(ns)
        token = resp.get("NextToken")
        if not token or len(nss) > 500: break
    return sorted(nss)

def list_metrics_in_namespace(ns: str, limit=200):
    """List metrics for a given namespace with a hard cap."""
    out, token = [], None
    while True:
        params = {"Namespace": ns}
        if token: params["NextToken"] = token
        resp = CW.list_metrics(**params)
        out.extend(resp.get("Metrics", []))
        token = resp.get("NextToken")
        if not token or len(out) >= limit: break
    return out[:limit]

def build_widget(metric: dict) -> dict:
    """Create a MetricWidget (GetMetricWidgetImage input)."""
    ns, name = metric["Namespace"], metric["MetricName"]
    dims = metric.get("Dimensions", [])
    pairs = []
    for d in dims:
        # Protect against malformed dimension dicts
        n = d.get("Name"); v = d.get("Value")
        if n is not None and v is not None:
            pairs += [n, v]
    return {
        "title": name,
        "view": "timeSeries",
        "stacked": False,
        "stat": "Average",
        "period": PERIOD_SECONDS,
        "metrics": [[ns, name] + pairs],
        "start": LOOKBACK_ISO,
        "end": "PT0M",
        "width": WIDGET_WIDTH,
        "height": WIDGET_HEIGHT
    }

def render_widget_png(widget: dict) -> bytes:
    """Render chart to PNG bytes. Never throws; logs and returns None on error."""
    try:
        resp = CW.get_metric_widget_image(MetricWidget=json.dumps(widget))
        return resp["MetricWidgetImage"]
    except (ClientError, BotoCoreError) as e:
        log_warn(f"Widget render failed for {widget.get('title')}: {e}")
        return None
    except Exception as e:
        log_warn(f"Widget render unexpected error for {widget.get('title')}: {e}\n{traceback.format_exc()}")
        return None

# ---------- Excel ----------
def build_excel(namespace: str, images: list) -> bytes:
    """
    Build a single Excel workbook containing all images for this namespace.
    images: list[(title:str, png_bytes:bytes)]
    """
    import xlsxwriter  # must be present via layer or vendored
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    # Main sheet
    sheet_name = safe(namespace)[:31] or "Sheet1"
    ws = wb.add_worksheet(sheet_name)

    # Layout heuristics
    col_chars = max(12, int((WIDGET_WIDTH * IMG_SCALE) / 7))  # roughly map px to excel char width
    for c in range(0, IMAGES_PER_ROW * 3): ws.set_column(c, c, col_chars)

    row, col, row_step = 0, 0, 20
    count = 0
    for i, (title, png) in enumerate(images, start=1):
        if not png:
            continue
        ws.set_row(row, int(WIDGET_HEIGHT * IMG_SCALE * 0.75))
        ws.insert_image(row, col, f"{safe(title)}.png",
                        {"image_data": io.BytesIO(png),
                         "x_scale": IMG_SCALE, "y_scale": IMG_SCALE})
        count += 1
        if (i % IMAGES_PER_ROW) == 0:
            row += row_step
            col = 0
        else:
            col += 3

    # Index sheet
    idx = wb.add_worksheet("Index")
    idx.write(0, 0, "Namespace"); idx.write(0, 1, "Images")
    idx.write(1, 0, namespace);   idx.write(1, 1, count)
    idx.write(3, 0, "Lookback");  idx.write(3, 1, LOOKBACK_ISO)
    idx.write(4, 0, "Period(s)"); idx.write(4, 1, PERIOD_SECONDS)

    wb.close(); buf.seek(0)
    return buf.read()

# ---------- Email ----------
def send_email_summary(ns_to_excel):
    """
    ns_to_excel: { ns: {"key": str, "bytes": bytes, "size_mb": float} }
    """
    msg = MIMEMultipart()
    msg["Subject"] = "CloudWatch Metrics – Excel Dashboards per Namespace"
    msg["From"] = SES_SENDER_EMAIL
    msg["To"] = ", ".join(SES_RECIPIENT_EMAILS)

    lines = [
        f"Generated Excel dashboards for time window {LOOKBACK_ISO} (period={PERIOD_SECONDS}s).",
        f"S3 bucket: {S3_BUCKET}", "", "Files:"
    ]
    for ns, info in ns_to_excel.items():
        lines.append(f"- {ns}: s3://{S3_BUCKET}/{info['key']} ({info['size_mb']:.2f} MB)")
    msg.attach(MIMEText("\n".join(lines), "plain"))

    if ATTACH_EXCEL:
        used = 0.0
        for ns, info in ns_to_excel.items():
            size = info["size_mb"]
            if used + size > MAX_EMAIL_MB:
                log_info(f"Skipping attachment {ns} ({size:.2f}MB) to stay under {MAX_EMAIL_MB}MB")
                continue
            part = MIMEApplication(info["bytes"])
            part.add_header("Content-Disposition", "attachment", filename=f"{safe(ns)}.xlsx")
            msg.attach(part)
            used += size

    SES.send_raw_email(
        Source=SES_SENDER_EMAIL,
        Destinations=SES_RECIPIENT_EMAILS,
        RawMessage={"Data": msg.as_string()}
    )

# ---------- Handler ----------
def lambda_handler(event, context):
    try:
        log_info(f"Region={boto3.session.Session().region_name} Lookback={LOOKBACK_ISO} Period={PERIOD_SECONDS}")
        log_info(f"Namespaces filter={NAMESPACES if NAMESPACES else 'auto-discover'}")

        targets = NAMESPACES if NAMESPACES else list_namespaces()
        if not targets:
            return {"status": "no_namespaces"}

        ns_to_excel, total_images = {}, 0

        for ns in targets:
            metrics = list_metrics_in_namespace(ns, MAX_METRICS_PER_NS)
            if not metrics:
                log_info(f"{ns}: no metrics found")
                continue

            images = []
            for m in metrics:
                widget = build_widget(m)
                png = render_widget_png(widget)
                if not png:
                    continue
                title = widget.get("title") or m.get("MetricName", "metric")
                images.append((title, png))
                total_images += 1

            if not images:
                log_info(f"{ns}: metrics found but no images rendered")
                continue

            excel_bytes = build_excel(ns, images)
            excel_key = f"cloudwatch/excel/{safe(ns)}.xlsx"
            s3_put(excel_key, excel_bytes,
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            ns_to_excel[ns] = {
                "key": excel_key,
                "bytes": excel_bytes,
                "size_mb": len(excel_bytes) / (1024 * 1024)
            }

        if not ns_to_excel:
            return {"status": "no_dashboards_generated"}

        send_email_summary(ns_to_excel)
        return {
            "status": "email_sent",
            "bucket": S3_BUCKET,
            "excel_files": {ns: info["key"] for ns, info in ns_to_excel.items()},
            "namespaces": list(ns_to_excel.keys()),
            "total_images": total_images
        }

    except Exception as e:
        log_err(f"Unhandled exception: {e}\n{traceback.format_exc()}")
        # Return error in payload for easier debugging in console
        return {"status": "error", "message": str(e)}
