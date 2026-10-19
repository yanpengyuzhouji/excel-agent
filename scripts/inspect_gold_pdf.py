"""Read and render service-produced PDFs using the bundled document runtime."""
import json
from pathlib import Path
import sys
from pypdf import PdfReader
import pypdfium2 as pdfium
from gold_v2_checks import text_checks

path=Path(sys.argv[1]);case=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
reader=PdfReader(path)
text='\n'.join(p.extract_text() or '' for p in reader.pages)
result={'pages':len(reader.pages),'text_chars':len(text),'checks':text_checks(case,text),'rendered':[]}
document=pdfium.PdfDocument(str(path))
for index in range(len(reader.pages)):
    if index<0:continue
    target=path.with_name(path.stem+f'-page-{index+1}.png')
    page=document[index];bitmap=page.render(scale=1.25);bitmap.to_pil().save(target)
    result['rendered'].append(str(target));bitmap.close();page.close()
document.close()
print(json.dumps(result,ensure_ascii=False))
