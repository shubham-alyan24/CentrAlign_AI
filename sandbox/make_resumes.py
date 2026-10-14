"""Generate the fictional resume PDFs used as email attachments (run once; output is committed).

    python -m sandbox.make_resumes
"""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

OUT = Path(__file__).parent / "seed" / "resumes"

RESUMES = {
    "Ananya_Sharma_Resume.pdf": {
        "name": "Ananya Sharma", "contact": "ananya.sharma@mailbox.test | +91 98765 43210 | Pune",
        "summary": "Backend engineer focused on payment APIs. Total professional experience: 3 years.",
        "notice": "Notice period: 30 days",
        "skills": "Python, FastAPI, PostgreSQL, Redis, Docker, AWS (ECS, SQS), REST API design, pytest",
        "experience": [
            "Software Engineer II, Fintrail Payments (2 years) - Built FastAPI services handling 2M+ UPI "
            "transactions/day; cut p95 latency by 38% with Redis caching.",
            "Software Engineer, Fintrail Payments (1 year) - Wrote reconciliation jobs in Python and PostgreSQL.",
        ],
        "education": "B.Tech, Computer Engineering - College of Engineering Pune",
    },
    "Rohit_Verma_CV.pdf": {
        "name": "Rohit Verma", "contact": "rohit.verma@mailbox.test | 9123456780 | Jaipur",
        "summary": "Junior backend developer. Total professional experience: 1.5 years.",
        "notice": "Notice period: 15 days",
        "skills": "Python, Django, Django REST Framework, MySQL, Git, Linux",
        "experience": [
            "Junior Developer, QuickKart (1.5 years) - Built order-tracking APIs in Django REST Framework; "
            "maintained MySQL schemas and admin dashboards.",
        ],
        "education": "B.Tech, Information Technology - Rajasthan Technical University",
    },
    "Karan_Malhotra_Resume.pdf": {
        "name": "Karan Malhotra", "contact": "karan.malhotra@mailbox.test | +91 98110 22334 | Gurugram",
        "summary": "Backend engineer building logistics platforms. Total professional experience: 2.5 years.",
        "notice": "Notice period: 45 days",
        "skills": "Python, Django, Celery, PostgreSQL, AWS (EC2, S3, Lambda), Docker, GitHub Actions",
        "experience": [
            "Software Engineer, CloudNest Logistics (2 years) - Owned the shipment-tracking service (Django + Celery); "
            "migrated batch jobs to AWS Lambda.",
            "Associate Engineer, ByteForge (6 months) - Built internal tooling in Python.",
        ],
        "education": "B.Tech, Computer Science - NIT Kurukshetra",
    },
    "Isha_Bansal_Resume.pdf": {
        "name": "Isha Bansal", "contact": "isha.bansal@mailbox.test | +91 99001 22446 | Delhi",
        "summary": "B2B sales professional. Total professional experience: 2 years.",
        "notice": "Notice period: 30 days",
        "skills": "B2B sales, lead qualification, HubSpot CRM, negotiation, Hindi/English",
        "experience": ["Sales Executive, OfficeMart (2 years) - Closed 60+ SME accounts; 118% of annual quota."],
        "education": "BBA - University of Delhi",
    },
}


def build(fname: str, r: dict) -> None:
    ss = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(OUT / fname), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, title=f"{r['name']} - Resume", author=r["name"])
    story = [Paragraph(r["name"], ss["Title"]), Paragraph(r["contact"], ss["Normal"]), Spacer(1, 8),
             Paragraph("Summary", ss["Heading2"]), Paragraph(r["summary"], ss["Normal"]),
             Paragraph(r["notice"], ss["Normal"]),
             Paragraph("Skills", ss["Heading2"]), Paragraph(r["skills"], ss["Normal"]),
             Paragraph("Experience", ss["Heading2"])]
    story += [Paragraph("&bull; " + e, ss["Normal"]) for e in r["experience"]]
    story += [Paragraph("Education", ss["Heading2"]), Paragraph(r["education"], ss["Normal"])]
    doc.build(story)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for f, r in RESUMES.items():
        build(f, r)
        print("wrote", OUT / f)
