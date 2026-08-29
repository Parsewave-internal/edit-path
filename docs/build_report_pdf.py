from pathlib import Path
import re, sys
sys.path.insert(0, '/tmp/reportlab-pkg/usr/lib/python3/dist-packages')
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted, PageBreak

src = Path(__file__).with_name('editpath-kdenlive-mlt-report.roff')
out = Path(__file__).with_name('editpath-kdenlive-mlt-report.pdf')
styles = getSampleStyleSheet()
title = ParagraphStyle('title', parent=styles['Title'], alignment=TA_CENTER, fontName='Helvetica-Bold', fontSize=20, leading=24, spaceAfter=8)
subtitle = ParagraphStyle('subtitle', parent=styles['Normal'], alignment=TA_CENTER, fontSize=11, leading=14, spaceAfter=16)
h1 = ParagraphStyle('h1', parent=styles['Heading1'], fontSize=15, leading=18, spaceBefore=12, spaceAfter=6)
h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=12, leading=15, spaceBefore=9, spaceAfter=4)
body = ParagraphStyle('body', parent=styles['BodyText'], fontSize=9.5, leading=13, spaceAfter=5)
bullet = ParagraphStyle('bullet', parent=body, leftIndent=12, firstLineIndent=-8)
code = ParagraphStyle('code', parent=styles['Code'], fontName='Courier', fontSize=7.5, leading=9.5, leftIndent=10, rightIndent=8, spaceBefore=4, spaceAfter=6)

story=[]; in_code=False; code_lines=[]
def text(s):
    s=s.replace('\\\\','\\').replace('\\f[CR]','').replace('\\fP','').replace('\\(bu','•').replace('\\(bu','•')
    s=s.replace('`','')
    return s
for raw in src.read_text().splitlines():
    line=raw.strip()
    if line=='.CODE': in_code=True; code_lines=[]; continue
    if line=='.END_CODE':
        story.append(Preformatted('\n'.join(code_lines), code)); in_code=False; continue
    if in_code:
        if line.startswith('.') and line not in ('.nf','.fi'): continue
        code_lines.append(line); continue
    if not line or line.startswith('."') or line.startswith('.nr ') or line.startswith('.ds '): continue
    if line.startswith('.H1 '): story.append(Paragraph(text(line[5:].strip('"')),h1)); continue
    if line.startswith('.H2 '): story.append(Paragraph(text(line[5:].strip('"')),h2)); continue
    if line.startswith('.ce') or line in ('.sp 1','.sp .3','.sp .5','.sp .2','.sp .6','.sp 2'): continue
    if line.startswith('.B '): story.append(Paragraph(text(line[3:].strip('"')),title)); continue
    if line.startswith('.I '): story.append(Paragraph(text(line[3:].strip('"')),subtitle)); continue
    if line.startswith('.IP '):
        value=line[4:].strip(); value=re.sub(r'^\\\(bu\s+\d+\s*','• ',value); value=re.sub(r'^\d+\s+\d+\s*','',value)
        if value: story.append(Paragraph(text(value), bullet))
        continue
    if line.startswith(('.sp','.nf','.fi','.ft','.in','.bp','.de','.')): continue
    story.append(Paragraph(text(line), body))

def footer(canvas, doc):
    canvas.saveState(); canvas.setFont('Helvetica',7); canvas.setFillGray(.45)
    canvas.drawString(18*mm, 10*mm, 'EditPath / Kdenlive / Melt-MLT technical report')
    canvas.drawRightString(192*mm, 10*mm, f'Page {doc.page}')
    canvas.restoreState()

doc=SimpleDocTemplate(str(out), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm, title='EditPath in Kdenlive')
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(out)
