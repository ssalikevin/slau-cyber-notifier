import logging
import math
import os
import sys
from datetime import date
from html import escape as html_escape
from io import BytesIO
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from PIL import Image as PILImage, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


app = FastAPI(
    title="SLAU Cyber Security & Innovations Club Portal",
    description="Advanced Administrative Terminal for Official Notice Dispatch",
    version="1.0.0",
)

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")
TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
STATIC_DIR = os.path.join(FRONTEND_DIR, "static")
ASSETS_DIR = os.path.join(PROJECT_DIR, "assets")
AUTH_COOKIE_NAME = "slau_cyber_portal_user"
SESSION_MAX_AGE = 60 * 60 * 8
STAMP_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


load_dotenv(os.path.join(PROJECT_DIR, ".env"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _render_template(request: Request, template_name: str, **context: object):
    template_context = {"request": request}
    template_context.update(context)
    return templates.TemplateResponse(request, template_name, template_context)


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=303)


def _current_user(request: Request) -> str | None:
    return request.cookies.get(AUTH_COOKIE_NAME)


def _local_login_email() -> str:
    return os.getenv("PORTAL_ADMIN_EMAIL", "").strip()


def _local_login_password() -> str:
    return os.getenv("PORTAL_ADMIN_PASSWORD", "").strip()


def _login_redirect(user_value: str) -> RedirectResponse:
    response = _redirect("/dashboard")
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=user_value,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return response


def _login_message_redirect(message: str) -> RedirectResponse:
    return _redirect(f"/login?{urlencode({'login_error': message})}")


def _configured_login() -> bool:
    return bool(_local_login_email() and _local_login_password())


def _credentials_are_valid(login_value: str, password_value: str) -> bool:
    if _configured_login():
        return login_value.lower() == _local_login_email().lower() and password_value == _local_login_password()
    return bool(login_value and password_value)


def _safe_filename(value: str, fallback: str = "notice") -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_")).strip("._-")
    return cleaned or fallback


def _asset_path(filename: str) -> str:
    return os.path.join(ASSETS_DIR, filename)


def _trim_transparent_image(path: str) -> PILImage.Image:
    with PILImage.open(path) as image:
        rgba_image = image.convert("RGBA")
        bbox = rgba_image.getchannel("A").getbbox()
        if bbox:
            rgba_image = rgba_image.crop(bbox)
        return rgba_image


def _load_stamp_font(size: int):
    for font_path in STAMP_FONT_CANDIDATES:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _image_or_spacer(
    filename: str,
    width: float,
    height: float,
    *,
    trim_transparency: bool = False,
    h_align: str = "CENTER",
):
    path = _asset_path(filename)
    if os.path.exists(path):
        if trim_transparency:
            try:
                rgba_image = _trim_transparent_image(path)
                buffer = BytesIO()
                rgba_image.save(buffer, format="PNG")
                buffer.seek(0)
                return RLImage(buffer, width=width, height=height, hAlign=h_align)
            except Exception:
                logger.debug("Falling back to untrimmed image for %s", filename, exc_info=True)
        return RLImage(path, width=width, height=height, hAlign=h_align)
    return Spacer(width, height)


def _stamp_image_with_date(
    stamp_date: str,
    width: float,
    height: float,
    *,
    h_align: str = "CENTER",
):
    path = _asset_path("stamp.png")
    if not os.path.exists(path):
        return Spacer(width, height)

    try:
        base_image = _trim_transparent_image(path)
        scale = 4
        canvas_size = (base_image.width * scale, base_image.height * scale)
        resized_stamp = base_image.resize(canvas_size, PILImage.Resampling.LANCZOS)

        composed = PILImage.new("RGBA", canvas_size, (255, 255, 255, 0))
        composed.paste(resized_stamp, (0, 0), resized_stamp)

        draw = ImageDraw.Draw(composed)
        font_size = max(44, int(canvas_size[1] * 0.05))
        font = _load_stamp_font(font_size)
        draw.text(
            (canvas_size[0] / 2, canvas_size[1] * 0.58),
            stamp_date,
            font=font,
            fill=(31, 61, 102, 235),
            anchor="mm",
        )

        buffer = BytesIO()
        composed.save(buffer, format="PNG")
        buffer.seek(0)
        return RLImage(buffer, width=width, height=height, hAlign=h_align)
    except Exception:
        logger.debug("Falling back to plain stamp for %s", stamp_date, exc_info=True)
        return _image_or_spacer("stamp.png", width, height, trim_transparency=True, h_align=h_align)


def _build_notice_pdf(
    ref_number: str,
    notice_date: str,
    audience: str,
    subject: str,
    body_text: str,
) -> bytes:
    parsed_date = date.fromisoformat(notice_date)
    display_date = parsed_date.strftime("%d/%m/%Y")
    stamp_date = parsed_date.strftime("%d %b %Y").upper()
    estimated_body_lines = max(body_text.count("\n") + 1, math.ceil(len(body_text) / 70))
    footer_gap = 3.2 * cm if estimated_body_lines <= 12 else 2.0 * cm if estimated_body_lines <= 18 else 1.0 * cm

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ClubTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=16,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0f172a"),
    )
    office_style = ParagraphStyle(
        "NoticeOffice",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.6,
        leading=10.2,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#1e3a5f"),
    )
    meta_label_style = ParagraphStyle(
        "NoticeMetaLabel",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.3,
        leading=10,
        textColor=colors.HexColor("#0f172a"),
    )
    meta_value_style = ParagraphStyle(
        "NoticeMetaValue",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.2,
        leading=11.5,
        textColor=colors.HexColor("#111827"),
    )
    subject_style = ParagraphStyle(
        "NoticeSubject",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0f172a"),
    )
    body_style = ParagraphStyle(
        "NoticeBody",
        parent=styles["BodyText"],
        fontName="Times-Roman",
        fontSize=11,
        leading=15.5,
        alignment=TA_JUSTIFY,
        textColor=colors.HexColor("#111827"),
    )
    signature_name_style = ParagraphStyle(
        "NoticeSignatureName",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.8,
        leading=9.8,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0f172a"),
    )
    signature_title_style = ParagraphStyle(
        "NoticeSignatureTitle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7.4,
        leading=8.5,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#475569"),
    )

    def _decorate_page(canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
        canvas.setLineWidth(1)
        canvas.rect(
            doc.leftMargin / 2,
            doc.bottomMargin / 2,
            width - doc.leftMargin,
            height - doc.bottomMargin,
            stroke=1,
            fill=0,
        )
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(width - doc.rightMargin, 0.95 * cm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    ref_label = html_escape(ref_number.strip())
    audience_label = html_escape(audience.strip())
    subject_label = html_escape(subject.strip())
    body_label = html_escape(body_text.strip()).replace("\n", "<br/>")

    header_table = Table(
        [
            [
                _image_or_spacer("club_logo.png", 2.0 * cm, 2.0 * cm),
                Paragraph(
                    "Cyber Security & Innovations Club<br/>"
                    "St. Lawrence University<br/>"
                    "<font size='7.5'>ssalikevin515@gmail.com</font>",
                    title_style,
                ),
                _image_or_spacer("university_logo.png", 2.0 * cm, 2.0 * cm),
            ]
        ],
        colWidths=[2.6 * cm, 12.8 * cm, 2.6 * cm],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, -1), 0.9, colors.HexColor("#0f172a")),
            ]
        )
    )

    office_line = Paragraph("OFFICE OF THE HEAD OF PROJECTS", office_style)

    reference_table = Table(
        [[Paragraph(f"<b>REF:</b> {ref_label}", meta_value_style), Paragraph(f"<b>DATE:</b> {display_date}", meta_value_style)]],
        colWidths=[8.85 * cm, 8.85 * cm],
    )
    reference_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    recipient_table = Table(
        [[Paragraph("TO:", meta_label_style), Paragraph(audience_label, meta_value_style)]],
        colWidths=[2.2 * cm, 15.8 * cm],
    )
    recipient_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    subject_table = Table(
        [[Paragraph("SUBJECT:", meta_label_style), Paragraph(subject_label, meta_value_style)]],
        colWidths=[2.2 * cm, 15.8 * cm],
    )
    subject_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    signature_table = Table(
        [
            [
                [
                    _image_or_spacer(
                        "signature.png",
                        3.95 * cm,
                        1.56 * cm,
                        trim_transparency=True,
                        h_align="CENTER",
                    ),
                    Spacer(1, 0.08 * cm),
                    Paragraph("SSALI KEVIN", signature_name_style),
                    Paragraph("Head of Projects", signature_title_style),
                ],
                [
                    _stamp_image_with_date(
                        stamp_date,
                        3.0 * cm,
                        3.0 * cm,
                        h_align="CENTER",
                    ),
                ],
            ]
        ],
        colWidths=[9.2 * cm, 6.8 * cm],
        hAlign="CENTER",
    )
    signature_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, 0), "CENTER"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    elements = [
        header_table,
        Spacer(1, 0.12 * cm),
        office_line,
        Spacer(1, 0.28 * cm),
        reference_table,
        Spacer(1, 0.18 * cm),
        recipient_table,
        Spacer(1, 0.12 * cm),
        subject_table,
        Spacer(1, 0.34 * cm),
        Paragraph(subject_label.upper(), subject_style),
        Spacer(1, 0.28 * cm),
        Paragraph(body_label or "&nbsp;", body_style),
        Spacer(1, footer_gap),
        signature_table,
    ]

    document.build(elements, onFirstPage=_decorate_page, onLaterPages=_decorate_page)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


@app.get("/", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
async def serve_login(request: Request):
    if _current_user(request):
        return _redirect("/dashboard")
    return _render_template(
        request,
        "login.html",
        login_error=request.query_params.get("login_error"),
    )


@app.post("/")
@app.post("/login")
async def handle_login(
    email: str | None = Form(None),
    username: str | None = Form(None),
    password: str = Form(...),
):
    login_value = (email or username or "").strip()
    password_value = password.strip()

    if not login_value or not password_value:
        return _login_message_redirect("Enter both email and password.")
    
    if login_value != "ssalikevin515@gmail.com" or password_value != "@headofProjects":
        return _login_message_redirect("Invalid email or password.")

    logger.info("Login attempt received for %s", login_value)
    return _login_redirect(login_value)


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    portal_user = _current_user(request)
    if not portal_user:
        return _redirect("/login")
    return _render_template(request, "dashboard.html", portal_user=portal_user)


@app.get("/logout")
async def logout():
    response = _redirect("/login")
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.post("/generate-pdf")
async def generate_pdf(
    request: Request,
    ref_number: str = Form(...),
    notice_date: str = Form(...),
    audience: str = Form(...),
    subject: str = Form(...),
    body_text: str = Form(...),
):
    if not _current_user(request):
        return _redirect("/login")

    ref_value = ref_number.strip()
    audience_value = audience.strip()
    subject_value = subject.strip()
    body_value = body_text.strip()

    if not ref_value or not audience_value or not subject_value or not body_value:
        raise HTTPException(status_code=400, detail="All notice fields are required.")

    try:
        pdf_bytes = _build_notice_pdf(
            ref_number=ref_value,
            notice_date=notice_date,
            audience=audience_value,
            subject=subject_value,
            body_text=body_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid notice date.") from exc

    filename = f"{_safe_filename(ref_value)}.pdf"
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    logger.info("Generated notice PDF for %s", ref_value)
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


if __name__ == "__main__":
    import uvicorn

    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)