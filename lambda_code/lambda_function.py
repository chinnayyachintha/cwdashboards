# ===============================================================
# Lambda: CloudWatch Metrics → Excel Dashboards → S3 + Email
# ===============================================================
import os, io, json, re, boto3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from botocore.exceptions import ClientError, BotoCoreError
import xlsxwriter

# ---------------------------
# AWS clients
# ---------------------------
CW  = boto3.client("cloudwatch")
S3  = boto3.client("s3")
SES = boto3.client("ses")

# ---------------------------
# Environment variables
# ---------------------------
SES_SENDER_EMAIL     = os.environ["SES_SENDER_EMAIL"]
SES_RECIPIENT_EMAILS = [x.strip() for x in os.environ["SES_RECIPIENT_EMAILS"].split(",")]
S3_BUCKET            = os.environ["S3_BUCKET"]

# Optional tuning parameters
NAMESPACES        = [x.strip() for x in os.getenv("NAMESPACES", "").split(",") if x.strip()]
LOOKBACK_ISO      = os.getenv("LOOKBACK_ISO", "-PT24H")       # 24h lookback
PERIOD_SECONDS     = int(os.getenv("PERIOD_SECONDS", "300"))  # 5-minute period
MAX_METRICS_PER_NS = int(os.getenv("MAX_METRICS_PER_NS", "100"))
IMG_SCALE          = float(os.getenv("IMG_SCALE", "0.35"))
ATTACH_EXCEL       = os.getenv("ATTACH_EXCEL", "true").lower() == "true"
MAX_EMAIL_MB       = float(os.getenv("MAX_EMAIL_MB", "7"))
WIDGET_WIDTH       = 1067
WIDGET_HEIGHT      = 300


# ---------------------------
# Utility helpers
# ---------------------------
def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def list_namespaces():
    """Discover all metric namespaces in the account."""
    namespaces, token = set(), None
    while True:
        resp = CW.list_metrics(NextToken=token) if token else CW.list_metrics()
        for m in resp.get("Metrics", []):
            ns = m.get("Namespace")
            if ns:
                namespaces.add(ns)
        token = resp.get("NextToken")
        if not token or len(namespaces) > 500:
            break
    return sorted(namespaces)


def list_metrics_in_namespace(ns):
    """Fetch a capped list of metrics for one namespace."""
    out, token = [], None
    while True:
        params = {"Namespace": ns}
        if token:
            params["NextToken"] = token
        resp = CW.list_metrics(**params)
        out.extend(resp.get("Metrics", []))
        token = resp.get("NextToken")
        if not token or len(out) >= MAX_METRICS_PER_NS:
            break
    return out[:MAX_METRICS_PER_NS]


def build_widget(metric):
    """Create a MetricWidget JSON for rendering."""
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


def render_widget_image(widget):
    """Return PNG bytes for a given metric widget."""
    try:
        img = CW.get_metric_widget_image(MetricWidget=json.dumps(widget))["MetricWidgetImage"]
        return img
    except (ClientError, BotoCoreError) as e:
        print(f"[WARN] Failed to render {widget.get('title')}: {e}")
        return None


def build_excel(namespace, image_map):
    """Create an Excel workbook containing all metric charts for this namespace."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Dashboard")
    bold = wb.add_format({"bold": True})
    ws.write("A1", f"CloudWatch Metrics: {namespace}", bold)
    ws.write("A2", f"Lookback: {LOOKBACK_ISO}, Period: {PERIOD_SECONDS}s")

    row = 4
    for title, img_data in image_map:
        if img_data:
            ws.insert_image(row, 0, f"{safe(title)}.png", {"image_data": io.BytesIO(img_data), "x_scale": IMG_SCALE, "y_scale": IMG_SCALE})
            ws.write(row + 17, 0, title)
            row += 20

    wb.close()
    buf.seek(0)
    return buf.read()


def s3_put(key, data, content_type):
    S3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{S3_BUCKET}/{key}"


def send_email(ns_to_excel):
    """Send SES email with summary and optional attachments."""
    msg = MIMEMultipart()
    msg["Subject"] = "CloudWatch Metrics Excel Dashboards"
    msg["From"] = SES_SENDER_EMAIL
    msg["To"] = ", ".join(SES_RECIPIENT_EMAILS)

    lines = ["CloudWatch Excel Dashboards have been generated.", ""]
    total_size = 0
    for ns, info in ns_to_excel.items():
        lines.append(f"- {ns}: {info['s3_path']} ({info['size_mb']:.2f} MB)")
    msg.attach(MIMEText("\n".join(lines), "plain"))

    if ATTACH_EXCEL:
        for ns, info in ns_to_excel.items():
            if total_size + info["size_mb"] > MAX_EMAIL_MB:
                print(f"[INFO] Skipping {ns} (too large to attach)")
                continue
            part = MIMEApplication(info["bytes"])
            part.add_header("Content-Disposition", "attachment", filename=f"{safe(ns)}.xlsx")
            msg.attach(part)
            total_size += info["size_mb"]

    SES.send_raw_email(
        Source=SES_SENDER_EMAIL,
        Destinations=SES_RECIPIENT_EMAILS,
        RawMessage={"Data": msg.as_string()}
    )


# ---------------------------
# Lambda handler
# ---------------------------
def lambda_handler(event, context):
    print(f"[INFO] Start Lambda in region {boto3.session.Session().region_name}")
    namespaces = NAMESPACES if NAMESPACES else list_namespaces()
    if not namespaces:
        return {"status": "no_namespaces"}

    ns_to_excel, total = {}, 0

    for ns in namespaces:
        metrics = list_metrics_in_namespace(ns)
        if not metrics:
            continue

        image_map = []
        for m in metrics:
            widget = build_widget(m)
            img_data = render_widget_image(widget)
            if img_data:
                image_map.append((widget["title"], img_data))
                total += 1

        if not image_map:
            continue

        excel_bytes = build_excel(ns, image_map)
        key = f"cloudwatch/excel/{safe(ns)}.xlsx"
        s3_path = s3_put(key, excel_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        ns_to_excel[ns] = {
            "bytes": excel_bytes,
            "s3_path": s3_path,
            "size_mb": len(excel_bytes) / (1024 * 1024),
        }

    if not ns_to_excel:
        return {"status": "no_excel_files_generated"}

    send_email(ns_to_excel)
    return {
        "status": "email_sent",
        "bucket": S3_BUCKET,
        "namespaces": list(ns_to_excel.keys()),
        "total_metrics": total,
    }
