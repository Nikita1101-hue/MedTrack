# MedTrack

**Smart Healthcare Management, Connected Through the Cloud.**

MedTrack is a cloud-based healthcare management web application. Patients book appointments, doctors confirm them and record diagnoses, and administrators oversee the whole system. Data is stored in **Amazon DynamoDB**, the app runs on **AWS EC2**, and **AWS IAM** controls what the app may access.

## Features

- **Patient:** register, log in, edit profile, browse/search doctors, book and cancel appointments, view diagnosis records (read-only), view notifications
- **Doctor:** log in, edit profile, confirm / reject / cancel appointments, view patient details, add diagnosis + prescription + notes (this completes the appointment), view previous records, send notifications
- **Admin:** dashboard, view/remove patients, add/remove doctors, view/cancel appointments, view all diagnosis records and system notifications
- Notifications on: booked, confirmed, rejected, cancelled, completed, diagnosis added
- Security: hashed passwords, Flask sessions, role-based access, CSRF tokens, input validation, friendly error pages
- Works with **no JavaScript** - every action is an HTML form posting to a Flask route

## Technology stack

HTML + CSS (Jinja2 templates) | Python 3 + Flask | Amazon DynamoDB (boto3) | AWS EC2 | AWS IAM

No React, Node.js, Bootstrap, SQL databases or JavaScript frameworks are used.

## Architecture

```
 Browser (HTML pages)
        | HTTP
        v
 Flask backend (app.py)  on  AWS EC2
        | boto3   (permissions come from an IAM role)
        v
 Amazon DynamoDB (6 tables)
```

## Folder structure

```
MedTrack/
├── app.py              # all routes, auth, database layer
├── requirements.txt
├── README.md
├── .env.example        # copy to .env (never commit .env)
├── .gitignore
├── templates/          # base.html, public pages, patient_*, doctor_*, admin_*, errors
└── static/images/      # logo
```

Two small helper templates (`_notifications.html`, `_appt_actions.html`) avoid repeating the same HTML.

## Installation

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Environment configuration

```bash
cp .env.example .env              # Windows: copy .env.example .env
```

| Variable | Meaning |
|---|---|
| `APP_MODE` | `development` (local JSON file, no AWS) or `production` (DynamoDB) |
| `SECRET_KEY` | Long random string. Required in production |
| `AWS_REGION` | e.g. `ap-south-1` |
| `USE_DYNAMODB` | `true` to use DynamoDB while in development mode |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | First admin account. Required in production |
| `SESSION_COOKIE_SECURE` | `true` only when served over HTTPS |

## Local execution (no AWS needed)

```bash
python app.py
```

Open http://127.0.0.1:5000

In development mode data is saved in `data/medtrack_local.json` (git-ignored) and the following are created automatically:

| Role | Email | Password |
|---|---|---|
| Admin | admin@medtrack.com | Admin@123 |
| Doctors (3 demo) | ananya@medtrack.com, rohan@medtrack.com, priya@medtrack.com | Doctor@123 |

Patients register themselves on the Register page. **These demo accounts exist only in development mode.**

## AWS configuration

### 1. DynamoDB setup

The app uses six tables, each with a string partition key and on-demand billing:

| Table | Partition key |
|---|---|
| MedTrack_Users | user_id |
| MedTrack_Patients | patient_id |
| MedTrack_Doctors | doctor_id |
| MedTrack_Appointments | appointment_id |
| MedTrack_Diagnosis | diagnosis_id |
| MedTrack_Notifications | notification_id |

Create them automatically (needs `dynamodb:CreateTable`, so run it with an admin-level identity, e.g. `aws configure` on your laptop, not with the app's role):

```bash
APP_MODE=production SECRET_KEY=x ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='StrongPass#1' python app.py init-db
```

Or create them by hand in the DynamoDB console with the names and keys above.

### 2. IAM role for EC2 (no access keys in code)

1. IAM -> Policies -> Create policy -> JSON (replace `REGION` and `ACCOUNT_ID`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:Scan"
    ],
    "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/MedTrack_*"
  }]
}
```

2. IAM -> Roles -> Create role -> trusted entity **EC2** -> attach the policy -> name it `MedTrackEC2Role`.
3. Attach the role to your instance: EC2 -> Instance -> Actions -> Security -> Modify IAM role.

boto3 automatically picks up the role's temporary credentials. Nothing secret goes in the code or in GitHub.

## EC2 deployment

1. **Launch** an Ubuntu 22.04/24.04 instance (t2/t3.micro is fine) with `MedTrackEC2Role` attached. In the security group allow **SSH (22)** from your IP and **Custom TCP 5000** (or HTTP 80 if you add a reverse proxy).
2. **Connect and install:**
   ```bash
   sudo apt update && sudo apt install -y python3-pip python3-venv git
   git clone https://github.com/<you>/MedTrack.git && cd MedTrack
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Configure:**
   ```bash
   cp .env.example .env && nano .env
   ```
   Set `APP_MODE=production`, a real `SECRET_KEY`, `AWS_REGION`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`. Do **not** add AWS keys - the IAM role provides access.
4. **Create tables** (once, from a machine with admin rights - see DynamoDB setup).
5. **Run:**
   ```bash
   python app.py
   ```
   Browse to `http://<EC2-public-IP>:5000`.

**Keeping it running / production-grade (optional):** `pip install gunicorn`, then `gunicorn -w 2 -b 0.0.0.0:5000 app:app` (wrap it in a systemd service so it restarts on reboot). For HTTPS put Nginx + a certificate in front and set `SESSION_COOKIE_SECURE=true`.

## Security considerations

- Passwords hashed with Werkzeug (`generate_password_hash` / `check_password_hash`); never stored in plain text
- Flask signed, HTTP-only sessions with a 2-hour lifetime; session reset on login
- Every protected route checks login and role; users deleted by an admin lose access immediately
- Patients can only see/cancel their own appointments and read their own diagnoses; doctors can only act on their own appointments
- CSRF token on every POST form; logout is a POST
- Input validation (email, phone, dates, time slots, lengths); Jinja2 auto-escapes output
- Admin accounts cannot be self-registered
- No AWS credentials in code; `.env` is in `.gitignore`; IAM policy limited to the six tables and five actions
- Users see friendly error pages, never stack traces (debug mode is for development only)

## Future scope

- DynamoDB Global Secondary Index on `email`, `patient_id`, `doctor_id` to replace table scans at scale
- Email/SMS alerts with Amazon SES / SNS
- Password reset, account lockout after repeated failures
- Doctor working-hours based slot generation, appointment rescheduling
- File upload of lab reports to Amazon S3
- HTTPS with Nginx + Let's Encrypt, Auto Scaling and a load balancer
