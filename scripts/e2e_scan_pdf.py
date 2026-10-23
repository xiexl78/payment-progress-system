# -*- coding: utf-8 -*-
"""扫描件 PDF 端到端测试"""
import os
import sys
import requests

BASE = 'http://127.0.0.1:5001'

def login(session, username, password):
    r = session.get(BASE + '/login')
    r = session.post(BASE + '/login', data={'username': username, 'password': password}, allow_redirects=False)
    return r.status_code in (302, 303)

def check(label, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print('  [%s] %s%s' % (status, label, (' - ' + detail) if detail else ''))
    return cond

def main():
    # 1. 创建扫描件 PDF
    from PIL import Image, ImageDraw, ImageFont
    sample_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts', 'samples')
    os.makedirs(sample_dir, exist_ok=True)
    
    img = Image.new('RGB', (1200, 1600), color='white')
    draw = ImageDraw.Draw(img)
    font_paths = [
        'C:/Windows/Fonts/simhei.ttf',
        'C:/Windows/Fonts/simsun.ttc',
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/msyhbd.ttc',
    ]
    font = None
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, 36)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()
    
    lines = [
        '合同总金额人民币壹拾陆万伍仟元整（￥165,000.00）',
        '税率：13%',
        '付款方式：首付30%预付款，进度款52%，验收款15%，质保金3%',
    ]
    y = 50
    for line in lines:
        draw.text((50, y), line, fill='black', font=font)
        y += 60
    
    img_path = os.path.join(sample_dir, 'scan_e2e.png')
    img.save(img_path)
    
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    rect = fitz.Rect(50, 50, 545, 400)
    page.insert_image(rect, filename=img_path)
    scan_pdf = os.path.join(sample_dir, 'scan_e2e.pdf')
    doc.save(scan_pdf)
    doc.close()
    print('创建扫描件PDF:', scan_pdf)
    
    # 2. 登录并上传
    s = requests.Session()
    login(s, 'admin', 'admin123')
    print('admin 登录 OK')
    
    with open(scan_pdf, 'rb') as f:
        r = s.post(BASE + '/contracts/upload', files={'file': ('scan_e2e.pdf', f, 'application/pdf')}, allow_redirects=False)
    
    print('  上传状态码:', r.status_code)
    print('  Location:', r.headers.get('Location', ''))
    if r.status_code not in (302, 303):
        print('  响应:', r.text[:500])
        return
    
    loc = r.headers.get('Location', '')
    if '/contracts/' not in loc or '/confirm' not in loc:
        print('  未重定向到确认页，停止测试')
        return
    ct_id = int(loc.rstrip('/').split('/')[-2])
    print('  合同ID:', ct_id)
    
    # 3. 检查确认页
    r2 = s.get(BASE + loc)
    ok = check('确认页 200', r2.status_code == 200)
    if ok:
        check('确认页含识别金额 165,000.00', '165,000.00' in r2.text)
        check('确认页含税率 13%', '13%' in r2.text)
        check('确认页含付款方式分期支付', '分期支付' in r2.text or '首付' in r2.text)
    
    # 4. 确认生成项目
    r3 = s.post(BASE + loc, data={
        'contract_name': '扫描件PDF测试项目',
        'original_amount': '165000',
        'additional_amount': '0',
        'tax_rate': '13%',
        'payment_method': '分期支付',
        'ratio_first': '25',
        'ratio_progress': '52',
        'ratio_acceptance': '15',
        'ratio_warranty': '3',
    }, allow_redirects=False)
    
    ok = check('扫描件PDF确认生成重定向', r3.status_code in (302, 303))
    if ok:
        loc3 = r3.headers.get('Location', '')
        print('  跳转:', loc3)
        # 检查项目详情页
        r4 = s.get(BASE + loc3)
        check('项目详情页 200', r4.status_code == 200)
        check('项目详情含扫描件PDF测试项目', '扫描件PDF测试项目' in r4.text)
        check('项目详情含4个付款节点', r4.text.count('首付款') >= 1)
    
    # 5. 检查合同记录状态
    r5 = s.get(BASE + '/contracts')
    if r5.status_code == 200:
        check('合同列表含扫描件记录', 'scan_e2e.pdf' in r5.text)
    
    # 清理
    for f in [scan_pdf, img_path]:
        try:
            os.remove(f)
        except Exception:
            pass
    print('清理临时文件')

if __name__ == '__main__':
    main()
