from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.units import inch
from typing import List
from .models import Transaction
from datetime import datetime
import os

# --- Define paths to your logo files ---
# Assumes an 'assets' folder at the project root
ASSIST_LOGO_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'icon_nobg_okanassist.png')
FIT_LOGO_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'okanfit_logo_500x.png')

def _footer_with_logo(canvas, doc):
    """
    Draws a footer with the OkanFit logo on each page.
    """
    canvas.saveState()
    if os.path.exists(FIT_LOGO_PATH):
        # Draw the OkanFit logo image, centered
        logo_width = 0.5 * inch
        logo_height = 0.5 * inch # Adjust height to maintain aspect ratio
        x_pos = doc.width / 2 + doc.leftMargin - (logo_width / 2)
        y_pos = doc.bottomMargin / 2
        canvas.drawImage(FIT_LOGO_PATH, x_pos, y_pos, width=logo_width, height=logo_height, mask='auto')
    canvas.restoreState()

def create_transaction_report_pdf(transactions: List[Transaction], user_name: str, start_date: datetime, end_date: datetime) -> str:
    """Generates a PDF report for a list of transactions and returns the file path."""
    
    file_path = f"/tmp/transaction_report_{user_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    doc = SimpleDocTemplate(file_path, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    # --- 1. Header: OkanAssist Logo (First Page Only) ---
    if os.path.exists(ASSIST_LOGO_PATH):
        logo = Image(ASSIST_LOGO_PATH, width=1*inch, height=1*inch)
        logo.hAlign = 'CENTER'
        elements.append(logo)
        elements.append(Spacer(1, 12))

    # Title
    title = Paragraph(f"Transaction Report for {user_name}", styles['h1'])
    title.style.alignment = 1 # Center align
    elements.append(title)
    
    # Date Range
    date_range = Paragraph(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}", styles['h3'])
    date_range.style.alignment = 1 # Center align
    elements.append(date_range)
    elements.append(Spacer(1, 24))

    # Data for the table
    data = [["Date", "Description", "Category", "Type", "Amount"]]
    total_income = 0
    total_expense = 0

    for t in sorted(transactions, key=lambda x: x.date):
        amount_str = f"${t.amount:,.2f}"
        if t.transaction_type.value == 'income':
            total_income += t.amount
        else:
            total_expense += t.amount
            
        data.append([
            t.date.strftime('%Y-%m-%d'),
            t.description,
            t.category,
            t.transaction_type.value.title(),
            amount_str
        ])

    # Summary Row
    net_flow = total_income - total_expense
    summary_data = [
        ["", "", "", "Total Income:", f"${total_income:,.2f}"],
        ["", "", "", "Total Expense:", f"${total_expense:,.2f}"],
        ["", "", "", "Net Flow:", f"${net_flow:,.2f}"]
    ]
    data.extend(summary_data)

    # Create Table
    table = Table(data)
    style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4A90E2')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -4), colors.HexColor('#F5F5F5')),
        ('GRID', (0, 0), (-1, -4), 1, colors.black),
        # Summary styles
        ('FONTNAME', (3, -3), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (3, -3), (3, -1), 'RIGHT'),
        ('BACKGROUND', (0, -3), (-1, -1), colors.white),
        ('GRID', (3, -3), (-1, -1), 1, colors.black),
        ('TOPPADDING', (0, -3), (-1, -1), 6),
    ])
    table.setStyle(style)
    elements.append(table)

    # --- 2. Build the doc with the OkanFit footer on all pages ---
    doc.build(elements, onFirstPage=_footer_with_logo, onLaterPages=_footer_with_logo)
    return file_path