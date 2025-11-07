import os
import json
import boto3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# AWS clients
CW  = boto3.client("cloudwatch")
S3  = boto3.client("s3")
SES = boto3.client("ses")

# Environment variables
SES_SENDER_EMAIL     = os.environ["SES_SENDER_EMAIL"]
SES_RECIPIENT_EMAILS = [x.strip() for x in os.environ["SES_RECIPIENT_EMAILS"].split(",")]
S3_BUCKET            = os.environ["S3_BUCKET"]

# Get all CloudWatch dashboards
def list_dashboards():
    names, token = [], None
    while True:
        resp = CW.list_dashboards(**({"NextToken": token} if token else {}))
        names += [d["DashboardName"] for d in resp.get("DashboardEntries", [])]
        token = resp.get("NextToken")
        if not token:
            break
    return names

# Get widgets from a dashboard
def get_widgets(dashboard_name):
    body = CW.get_dashboard(DashboardName=dashboard_name)["DashboardBody"]
    return json.loads(body).get("widgets", [])

# Generate metric widget image
def render_widget_image(widget):
    if "properties" not in widget:
        return None
    props = widget["properties"]
    img = CW.get_metric_widget_image(MetricWidget=json.dumps(props))["MetricWidgetImage"]
    title = props.get("title", "widget")
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in title)
    return f"{safe}.png", img

# Upload image to S3
def upload_to_s3(dashboard, filename, data):
    key = f"cloudwatch/{dashboard}/{filename}"
    S3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType="image/png")
    return key

# Read image from S3
def read_from_s3(key):
    obj = S3.get_object(Bucket=S3_BUCKET, Key=key)
    return obj["Body"].read()

# Main Lambda handler
def lambda_handler(event, context):
    dashboards = list_dashboards()
    if not dashboards:
        return {"status": "no_dashboards"}

    stored = {}

    # Generate and upload images
    for d in dashboards:
        widgets = get_widgets(d)
        keys = []
        for w in widgets:
            result = render_widget_image(w)
            if not result:
                continue
            filename, blob = result
            key = upload_to_s3(d, filename, blob)
            keys.append(key)
        if keys:
            stored[d] = keys

    if not stored:
        return {"status": "no_images_stored"}

    # Build email with attachments
    msg = MIMEMultipart()
    msg["Subject"] = "CloudWatch Dashboard Images"
    msg["From"] = SES_SENDER_EMAIL
    msg["To"] = ", ".join(SES_RECIPIENT_EMAILS)
    msg.attach(MIMEText("Attached are the latest CloudWatch dashboard widget images.", "plain"))

    # Attach images from S3
    for d, keys in stored.items():
        for key in keys:
            blob = read_from_s3(key)
            fname = f"{d}-{key.split('/')[-1]}"
            part = MIMEApplication(blob)
            part.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(part)

    # Send email
    SES.send_raw_email(
        Source=SES_SENDER_EMAIL,
        Destinations=SES_RECIPIENT_EMAILS,
        RawMessage={"Data": msg.as_string()}
    )

    return {"status": "email_sent", "bucket": S3_BUCKET, "dashboards": list(stored.keys())}
