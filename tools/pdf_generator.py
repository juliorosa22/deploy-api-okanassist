from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from typing import List
from .models import Transaction
from datetime import datetime

def create_transaction_report_pdf(transactions: List[Transaction], user_name: str, start_date: datetime, end_date: datetime) -> str:
    """Generates a PDF report for a list of transactions and returns the file path."""
    
    file_path = f"/tmp/transaction_report_{user_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    doc = SimpleDocTemplate(file_path, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    title = Paragraph(f"Transaction Report for {user_name}", styles['h1'])
    elements.append(title)
    
    # Date Range
    date_range = Paragraph(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}", styles['h3'])
    elements.append(date_range)
    elements.append(Spacer(1, 12))

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
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -4), colors.beige),
        ('GRID', (0, 0), (-1, -4), 1, colors.black),
        # Summary styles
        ('FONTNAME', (3, -3), (-1, -1), 'Helvetica-Bold'),
        ('ALIGN', (3, -3), (3, -1), 'RIGHT'),
    ])
    table.setStyle(style)
    elements.append(table)

    doc.build(elements)
    return file_path