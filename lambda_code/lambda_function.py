# ===============================================================
# Lambda: CloudWatch Metrics -> Per-Namespace Excel Dashboards -> S3 + Email
# ===============================================================
import os, io, json, re, boto3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from botocore.exceptions import ClientError

# ---------------------------
# AWS Clients
# ---------------------------
CW  = boto3.client("cloudwatch")
S3  = boto3.client("s3")
SES = boto3.client("ses")

# ---------------------------
# Environment Variables
# ---------------------------
SES_SENDER_EMAIL      = os.environ["SES_SENDER_EMAIL"]
SES_RECIPIENT_EMAILS  = [x.strip() for x in os.environ["SES_RECIPIENT_EMAILS"].split(",")]
S3_BUCKET             = os.environ["S3_BUCKET"]

# Optional settings
NAMESPACES            = [x.strip() for x in os.getenv("NAMESPACES", "").split(",") if x.strip()]
LOOKBACK_ISO          = os.getenv("LOOKBACK_ISO", "-PT24H")         # past 24h window
PERIOD_SECONDS        = int(os.getenv("PERIOD_SECONDS", "300"))     # 5 min granularity
MAX_METRICS_PER_NS    = int(os.getenv("MAX_METRICS_PER_NS", "200")) # per-namespace limit
IMAGES_PER_ROW        = int(os.getenv("IMAGES_PER_ROW", "2"))       # layout in Excel
IMG_SCALE             = float(os.getenv("IMG_SCALE", "0.35"))       # image scale
ATTACH_EXCEL          = os.getenv("ATTACH_EXCEL", "false").lower() == "true"
MAX_EMAIL_MB          = float(os.getenv("MAX_EMAIL_MB", "8"))       # max attach size per email (SES limit ~10 MB)

# ---------------------------
# Utility Functions
# ---------------------------
def safe(s: str) -> str:
    """Make a safe filename / sheet name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)

def s3_put(key: str, data: bytes, content_type: str):
    """Upload object to S3."""
    S3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return key

# ---------------------------
# CloudWatch Helpers
# ---------------------------
def list_namespaces():
    """Discover all CloudWatch metric namespaces."""
    namespaces, token = set(), None
    while True:
        resp = CW.list_metrics(NextToken=token) if token else CW.list_metrics()
        for m in resp.get("Metrics", []):
            ns = m.get("Namespace")
            if ns: namespaces.add(ns)
        token = resp.get("NextToken")
        if not token or len(namespaces) > 200: break
    return sorted(namespaces)

def list_metrics_in_namespace(ns: str, limit=200):
    """List metrics for a given namespace."""
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
    """Create a MetricWidget JSON body for rendering charts."""
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
        "width": 1067,
        "height": 300
    }

def render_widget_image(widget: dict) -> bytes:
    """Render metric chart to PNG bytes."""
    return CW.get_metric_widget_image(MetricWidget=json.dumps(widget))["MetricWidgetImage"]

# ---------------------------
# Excel Builder
# ---------------------------
def build_excel(namespace: str, images: list) -> bytes:
    """
    Create one Excel workbook for this namespace.
    Each entry in images = (metric_name, png_bytes).
    """
    import xlsxwriter
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    ws = wb.add_worksheet(safe(namespace)[:31])
    col_width = max(12, int((1067 * IMG_SCALE) / 7))
    for c in range(0, IMAGES_PER_ROW * 3): ws.set_column(c, c, col_width)

    row, col, step = 0, 0, 20
    count = 0

    for i, (title, png) in enumerate(images, start=1):
        ws.set_row(row, int(300 * IMG_SCALE * 0.75))
        ws.insert_image(
            row, col,
            f"{safe(title)}.png",
            {"image_data": io.BytesIO(png), "x_scale": IMG_SCALE, "y_scale": IMG_SCALE}
        )
        count += 1
        if (i % IMAGES_PER_ROW) == 0:
            row += step; col = 0
        else:
            col += 3

    index = wb.add_worksheet("Index")
    index.write(0, 0, "Namespace")
    index.write(0, 1, "Images")
    index.write(1, 0, namespace)
    index.write(1, 1, count)

    wb.close(); buf.seek(0)
    return buf.read()

# ---------------------------
# Email Sender
# ---------------------------
def send_email(summary, attach_data):
    """Send summary email via SES (optionally with Excel attachments)."""
    msg = MIMEMultipart()
    msg["Subject"] = "CloudWatch Metrics Excel Dashboards"
    msg["From"] = SES_SENDER_EMAIL
    msg["To"] = ", ".join(SES_RECIPIENT_EMAILS)

    lines = [
        f"CloudWatch metric dashboards generated for time window {LOOKBACK_ISO}.",
        f"S3 Bucket: {S3_BUCKET}",
        "", "Dashboards:"
    ]
    for ns, key in summary.items():
        lines.append(f"- {ns}: s3://{S3_BUCKET}/{key}")
    msg.attach(MIMEText("\n".join(lines), "plain"))

    if ATTACH_EXCEL:
        used = 0.0
        for ns, (excel_bytes, size_mb) in attach_data.items():
            if used + size_mb > MAX_EMAIL_MB:
                print(f"[INFO] Skipping attach {ns} ({size_mb:.2f} MB) to keep under {MAX_EMAIL_MB} MB limit")
                continue
            part = MIMEApplication(excel_bytes)
            part.add_header("Content-Disposition", "attachment", filename=f"{safe(ns)}.xlsx")
            msg.attach(part)
            used += size_mb

    SES.send_raw_email(Source=SES_SENDER_EMAIL,
                       Destinations=SES_RECIPIENT_EMAILS,
                       RawMessage={"Data": msg.as_string()})

# ---------------------------
# Lambda Handler
# ---------------------------
def lambda_handler(event, context):
    targets = NAMESPACES if NAMESPACES else list_namespaces()
    if not targets:
        return {"status": "no_namespaces_found"}

    excel_summary, attach_data = {}, {}
    total_images = 0

    for ns in targets:
        metrics = list_metrics_in_namespace(ns, MAX_METRICS_PER_NS)
        if not metrics: continue

        images = []
        for m in metrics:
            try:
                widget = build_widget(m)
                img = render_widget_image(widget)
                images.append((widget["title"], img))
                total_images += 1
            except ClientError as e:
                print(f"[WARN] {ns}: {e}")
                continue

        if not images:
            continue

        excel_bytes = build_excel(ns, images)
        excel_key = f"cloudwatch/excel/{safe(ns)}.xlsx"
        s3_put(excel_key, excel_bytes,
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        excel_summary[ns] = excel_key
        attach_data[ns] = (excel_bytes, len(excel_bytes) / (1024 * 1024))

    if not excel_summary:
        return {"status": "no_dashboards_generated"}

    send_email(excel_summary, attach_data)

    return {
        "status": "email_sent",
        "bucket": S3_BUCKET,
        "total_images": total_images,
        "dashboards": excel_summary
    }
