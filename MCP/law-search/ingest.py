
# =====================================================
# ingest.py V6.5
# 法律知识库构建系统
#
# 功能:
# - 多library
# - 多路径
# - metadata增强
# - 文件hash去重
# - PDF页码
# - 年份识别
# - 文档标题识别
# - Chroma持久化
#
# =====================================================


import os
import re
import json
import hashlib
import argparse
import datetime


import yaml
import chromadb


from tqdm import tqdm


from sentence_transformers import SentenceTransformer


from pypdf import PdfReader


from docx import Document

from pathlib import Path



# =====================================================
# 基础路径
# =====================================================


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)



# 路径与集合配置统一走 config/（唯一配置真相源）
from config import (
    CHROMA_PATH as CONFIG_CHROMA_PATH,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    HASH_INDEX_FILE as CONFIG_HASH_INDEX_FILE,
    PATHS_YAML,
)
from core.chroma_lock import chroma_write_lock, snapshot_after_write
from core.doc_classifier import classify_document

PATH_CONFIG = PATHS_YAML


CHROMA_PATH = CONFIG_CHROMA_PATH


HASH_INDEX_FILE = CONFIG_HASH_INDEX_FILE





# =====================================================
# 参数
# =====================================================


parser = argparse.ArgumentParser(
    description="法律知识库构建系统 V6.5"
)



parser.add_argument(
    "--library",
    nargs="+",
    help="指定知识库"
)



parser.add_argument(
    "--limit",
    type=int,
    default=None,
    help="限制处理文件数量"
)



parser.add_argument(
    "--test",
    action="store_true",
    help="测试扫描"
)



parser.add_argument(
    "--dry-run",
    action="store_true",
    help="只扫描不写入"
)



args = parser.parse_args()





# =====================================================
# 加载 paths.yaml
# =====================================================


def load_config():


    with open(

        PATH_CONFIG,

        "r",

        encoding="utf-8"

    ) as f:


        return yaml.safe_load(f)







config = load_config()



libraries = config.get(
    "libraries",
    {}
)





print(
    "\n发现知识库:"
)


print(
    list(
        libraries.keys()
    )
)







# =====================================================
# 当前处理library
# =====================================================


if args.library:


    selected_libraries = []


    for lib in args.library:


        if lib in libraries:


            selected_libraries.append(
                lib
            )

        else:


            print(
                "不存在:",
                lib
            )



else:


    selected_libraries = list(
        libraries.keys()
    )





print(
    "\n当前处理:"
)


print(
    selected_libraries
)





# =====================================================
# Embedding模型
# =====================================================


print(
    "\n加载 embedding 模型..."
)


# 统一走 config.EMBEDDING_MODEL + 本地 snapshot（禁止联网下载、
# 保证重建库与检索层用的是同一个模型）
from huggingface_hub import snapshot_download

_EMBEDDING_LOCAL_PATH = snapshot_download(
    repo_id=EMBEDDING_MODEL,
    local_files_only=True,
)

# 懒加载：dry-run/纯扫描不加载模型（避免模块级副作用）
_embedding_model = None


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        print("加载embedding模型...")
        _embedding_model = SentenceTransformer(
            _EMBEDDING_LOCAL_PATH,
            local_files_only=True,
        )
        print("embedding模型加载完成")
    return _embedding_model






# =====================================================
# Chroma
# =====================================================


client = chromadb.PersistentClient(
    path=CHROMA_PATH
)



print(
    "\nChroma路径:"
)


print(
    CHROMA_PATH
)






# =====================================================
# Hash索引
# =====================================================


if os.path.exists(
    HASH_INDEX_FILE
):


    try:

        with open(
            HASH_INDEX_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            hash_index = json.load(f)

    except Exception:

        print(
            "Hash索引损坏，重新建立"
        )

        hash_index = {}


else:


    hash_index = {}



print(
    "已完成文件:",
    len(hash_index)
)

# =====================================================
# 文件Hash
# =====================================================


def calculate_hash(path):


    md5 = hashlib.md5()



    try:


        with open(

            path,

            "rb"

        ) as f:


            for chunk in iter(

                lambda:f.read(1024*1024),

                b""

            ):


                md5.update(chunk)



        return md5.hexdigest()



    except Exception as e:


        print(
            "Hash失败:",
            path,
            e
        )


        return None







# =====================================================
# 年份识别
# =====================================================


def extract_year(filename):


    result = re.search(

        r"(19\d{2}|20\d{2})",

        filename

    )


    if result:


        return result.group()



    return None







# =====================================================
# 文档标题识别
# =====================================================


def extract_title(filename):


    name = os.path.splitext(
        filename
    )[0]


    # 去除常见编号


    name = re.sub(

        r"^[\d\-\._、\s]+",

        "",

        name

    )


    return name.strip()





# =====================================================
# Document Cleaner V1
# =====================================================


def clean_document_text(text):


    if not text:
        return ""


    text = text.strip()


    # 去除连续空白

    text = re.sub(
        r"\n\s*\n\s*\n+",
        "\n\n",
        text
    )


    # 无效页面关键词

    blacklist = [

        "图书在版编目",

        "CIP数据",

        "中国版本图书馆",

        "ISBN",

        "责任编辑",

        "装帧设计",

        "出版发行",

        "版权所有",

        "印刷",

        "定价",

        "目录",

    ]


    for word in blacklist:

        if word in text:

            return ""


    # 过短页面过滤

    if len(text) < 50:

        return ""


    return text





# =====================================================
# PDF读取
# =====================================================


def read_pdf(path):


    pages = []



    try:


        reader = PdfReader(
            path
        )


        for index,page in enumerate(
            reader.pages
        ):


            try:


                text = page.extract_text()



            except:


                text = ""



            clean_text = clean_document_text(
                text
            )


            if clean_text:


                pages.append(

                    {

                        "text":
                            clean_text,

                        "page":
                            index + 1

                    }

                )
                
            

    except Exception as e:


        print(


            "PDF读取失败:",


            path,


            e


        )





    # 扫描版 PDF fallback：文本层几乎为空时走 OCR（页数上限 50，防止长任务失控）

    if not pages:



        try:



            from core.ocr_service import extract_text_smart



            ocr_text, ocr_meta = extract_text_smart(


                path


            )



            if ocr_text:



                print(



                    "扫描版PDF已OCR:",



                    f"ocr_pages={ocr_meta.get('ocr_pages')},",



                    f"skipped={ocr_meta.get('skipped_pages')}"



                )



                pages.append(



                    {



                        "text":



                            clean_document_text(



                                ocr_text



                            ),



                        "page":



                            1



                    }



                )



        except Exception as ocr_e:



            print(



                "OCR失败:",



                path,



                ocr_e



            )





    return pages







# =====================================================
# DOCX读取
# =====================================================


def read_docx(path):


    result = []



    try:


        doc = Document(
            path
        )


        text = "\n".join(

            p.text

            for p in doc.paragraphs

            if p.text.strip()

        )



        text = clean_document_text(
            text
        )
    


        if text:


            result.append(

                {

                    "text":
                        text,

                    "page":
                        None

                }

            )



    except Exception as e:


        print(


            "DOCX读取失败:",


            path,


            e


        )





    return result





def read_office(path):



    """旧版/新版 Office 文件读取（doc/ppt/pptx/xls/xlsx → office_reader）。"""



    result = []



    try:



        from core.office_reader import extract_office_text



        text, office_meta = extract_office_text(



            path



        )



        cleaned = clean_document_text(text)
        if cleaned:

            result.append(
                {
                    "text":
                        cleaned,
                    "page":
                        None
                }
            )



        print(



            "Office读取:",



            office_meta.get("format"),



            f"字数={len(text) if text else 0}"



        )



    except Exception as e:



        print(



            "Office读取失败:",



            path,



            e



        )



    return result







# =====================================================
# TXT / Markdown读取
# =====================================================


def read_text(path):


    try:


        with open(

            path,

            "r",

            encoding="utf-8"

        ) as f:


            text = f.read()


            text = clean_document_text(text)


        if not text:
            return []


        return [
            {
                "text": text,
                "page": None
            }
        ]




    except Exception as e:


        print(

            "文本读取失败:",

            path,

            e

        )



        return []







# =====================================================
# 通用文件读取
# =====================================================


def read_document(path):


    ext = os.path.splitext(
        path
    )[1].lower()



    if ext == ".pdf":


        return read_pdf(
            path
        )



    elif ext == ".docx":


        return read_docx(
            path
        )



    elif ext in [
        ".doc",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx"
    ]:


        return read_office(
            path
        )


    elif ext in [

        ".txt",

        ".md"

    ]:


        return read_text(
            path
        )





    return []







# =====================================================
# 文件扫描
# =====================================================


# =====================================================
# 保密守卫（deny-by-default，2026-09-02）
#
# 原则：漏放一篇模板的代价是少个参考；
#       漏放一份案卷的代价是违反保密义务。
# 故客户材料区默认拒绝入库，仅显式白名单放行。
#
# L1 禁区：CONFIDENTIAL_DENY_ROOTS 整树拒绝，
#     CONFIDENTIAL_ALLOW_SUBDIRS 白名单子目录放行
#     （目录分拣完成后取消注释启用）；
# L2 兜底：禁区内（含白名单区）文件名命中证据类黑名单一律拒绝。
# 非禁区路径（Laws&Rules / 公众号 / 专业研究等公开语料）不受影响。
# =====================================================

CONFIDENTIAL_DENY_ROOTS = [
    "/Users/simon/Documents/1-Work/1-5 工作资料",
    # NAS MacBook Pro 备份副本（MAC Studio 经 SMB 挂载，1-5 目录名带空格）
    "/Volumes/sata11-138XXXX2122/文档同步/电脑备份/MacBook Pro/1-Work/1-5 工作资料",
]

CONFIDENTIAL_ALLOW_SUBDIRS = [
    # "01-文书模板与工作底稿",  # ← 目录分拣完成后取消注释启用
]

CONFIDENTIAL_FILENAME_DENY = re.compile(
    r"证据\s*\d|证据清单|证据目录|质证|庭审笔录|谈话笔录|"
    r"询问笔录|讯问笔录|银行流水|转账记录|微信记录|聊天记录|"
    r"往来函件|委托代理合同|授权委托书|身份证|营业执照"
)

CONFIDENTIAL_DENIED_STATS = {"count": 0, "samples": []}


def check_confidentiality(filepath):
    """保密守卫：返回 (allowed: bool, reason: str)。

    L1: 文件位于保密禁区 → 仅白名单子目录内放行；
    L2: 禁区内（含白名单区）文件名命中证据类黑名单 → 一律拒绝。
    """
    p = str(filepath)
    for root in CONFIDENTIAL_DENY_ROOTS:
        if not (p == root or p.startswith(root + os.sep)):
            continue
        # L2 文件名黑名单（对禁区内所有文件生效，白名单区也不例外）
        if CONFIDENTIAL_FILENAME_DENY.search(os.path.basename(p)):
            return False, "filename_hit"
        # L1 白名单子目录（按路径段精确匹配）
        rel = os.path.relpath(p, root)
        if any(sub in rel.split(os.sep) for sub in CONFIDENTIAL_ALLOW_SUBDIRS):
            return True, "whitelisted"
        return False, "deny_root"
    return True, "public_zone"


def _record_confidential_denial(filepath, reason):
    CONFIDENTIAL_DENIED_STATS["count"] += 1
    if len(CONFIDENTIAL_DENIED_STATS["samples"]) < 5:
        CONFIDENTIAL_DENIED_STATS["samples"].append(f"{filepath} ({reason})")


def scan_source(path):

    CONFIDENTIAL_DENIED_STATS.update({"count": 0, "samples": []})

    files = []



    if not os.path.exists(
        path
    ):


        print(

            "路径不存在:",

            path

        )


        return files



    # 文件级 source：原位引用单个文件（不改变原文件存储路径）

    if os.path.isfile(
        path
    ):


        if path.lower().endswith(
            (
                ".pdf",
                ".docx",
                ".doc",
                ".ppt",
                ".pptx",
                ".xls",
                ".xlsx",
                ".txt",
                ".md"
            )
        ):


            allowed, reason = check_confidentiality(path)

            if not allowed:

                _record_confidential_denial(path, reason)

                return files


            files.append(
                path
            )


        return files




    for root,dirs,names in os.walk(
        path
    ):


        for name in names:


            if name.lower().endswith(

                (

                    ".pdf",

                    ".docx",

                    ".doc",

                    ".ppt",

                    ".pptx",

                    ".xls",

                    ".xlsx",

                    ".txt",

                    ".md"

                )

            ):

                filepath = os.path.join(
                    root,
                    name
                )

                allowed, reason = check_confidentiality(filepath)

                if not allowed:

                    _record_confidential_denial(filepath, reason)

                    continue

                files.append(
                    filepath
                )



    return files


# =====================================================
# Document Cleaner V1.1
# 过滤目录、出版社信息、无效页面
# =====================================================


def clean_document_text(text):


    if not text:
        return ""


    remove_keywords = [

        "图书在版编目",

        "CIP数据",

        "中国国家版本馆",

        "ISBN",

        "出版社",

        "责任编辑",

        "出版发行",

        "印刷",

        "开本",

        "目录",

        "目　录",

    ]


    lines = text.splitlines()


    cleaned = []


    hit_count = 0


    for line in lines:


        line = line.strip()


        if not line:

            continue



        remove = False


        for kw in remove_keywords:


            if kw in line:

                remove = True

                hit_count += 1

                break



        if not remove:

            cleaned.append(line)



    result = "\n".join(cleaned)



    # 页面过短直接丢弃

    if len(result) < 50:

        return ""



    return result


# =====================================================
# 文档切片
# =====================================================


def split_text(
    text,
    chunk_size=800,
    overlap=100
):


    text = text.strip()



    if len(text) <= chunk_size:


        return [
            text
        ]



    chunks = []



    start = 0



    length = len(text)



    while start < length:


        end = start + chunk_size



        chunk = text[start:end]



        if end < length:


            # 尝试在段落或句号处分割


            pos = max(

                chunk.rfind("。"),

                chunk.rfind("；"),

                chunk.rfind("\n")

            )



            if pos > chunk_size * 0.5:


                end = start + pos + 1


                chunk = text[start:end]




        chunks.append(
            chunk.strip()
        )



        start = end - overlap



        if start < 0:


            start = 0



    return [

        c

        for c in chunks

        if len(c) > 30

    ]







# =====================================================
# 法律条文增强切分
# =====================================================


def split_law_text(text):


    articles = re.split(

        r"(第[一二三四五六七八九十百千万零〇0-9]+条)",

        text

    )



    result = []



    current = ""



    for item in articles:



        if re.match(

            r"第[一二三四五六七八九十百千万零〇0-9]+条",

            item

        ):


            if current.strip():

                result.append(
                    current.strip()
                )



            current = item



        else:


            current += item



    if current.strip():


        result.append(
            current.strip()
        )



    return result


# =====================================================
# 根据类型选择切片
# =====================================================


def make_chunks(
    text,
    library_type
):


    if library_type == "law":


        return split_law_text(
            text
        )



    else:


        return split_text(
            text
        )




# =====================================================
# 法条编号提取
# =====================================================

def extract_articles(text):


    # 兼容 PDF OCR:
    # 第一条
    # 第一 条
    # 第一　条
    # 第 一 条
    # 第10条
    # 第 10 条

    pattern = (
        r"第\s*"
        r"[一二三四五六七八九十百千万零〇0-9]+"
        r"\s*条"
    )


    articles = re.findall(
        pattern,
        text
    )


    result = []


    for article in articles:

        article = (
            article
            .replace(" ", "")
            .replace("\u3000", "")
            .replace("\n", "")
        )


        if article not in result:

            result.append(article)


    return result


def bind_articles_to_chunks(chunks):


    result = []


    current_article = ""


    for chunk in chunks:


        articles = extract_articles(
            chunk
        )


        if articles:

            current_article = ",".join(
                articles
            )


        result.append(
            {
                "text": chunk,
                "article": current_article
            }
        )


    return result

# =====================================================
# 法律引用解析 Citation Parser V1
# =====================================================


def extract_citation(
    chunk_text,
    filename
):

    citation = {

        "citation_name": "",
        "citation_article": "",
        "citation_type": "",
        "law_name": "",
        "source_document": "",
        "document_part": "",
        "document_subtype":""

    }


    text = chunk_text
    filename_text = str(filename)

    
    # ===================================
    # 章节标题识别
    # ===================================

    chapter_patterns = {

        "公司登记管理":
            "市场主体登记管理条例",

        "破产法司法解释":
            "企业破产法司法解释",

        "证券发行":
            "证券法",

    }    


    # =============================
    # 文档章节识别
    # =============================


    part_patterns = {


        "公司法":
            "中华人民共和国公司法",


        "公司登记管理":
            "市场主体登记管理条例",


        "证券发行":
            "证券法",


        "公司治理":
            "上市公司治理准则",


        "企业破产":
            "企业破产法",


    }


    law_type_patterns = {


        "中华人民共和国公司法":

        {
            "document_subtype":"法律正文",
            "law_level":"法律"
        },


        "最高人民法院关于适用":

        {
            "document_subtype":"司法解释",
            "law_level":"司法解释"
        },


        "上市公司章程指引":

        {
            "document_subtype":"部门规范",
            "law_level":"规范性文件"
        },


        "企业国有资产交易监督管理办法":

        {
            "document_subtype":"部门规章",
            "law_level":"部门规章"
        }

    }    


    for part,name in part_patterns.items():

        if part in text:

            citation["document_part"] = part

            break


    
    # =================================================
    # 汇编章节识别
    # =================================================


    section_patterns = {


    "一、综合":
    "综合",


    "二、公司登记管理":
    "公司登记管理",


    "三、公司证券与上市":
    "公司证券与上市",


    "四、公司治理":
    "公司治理",


    "五、股东权益":
    "股东权益",


    "六、公司诉讼":
    "公司诉讼",


    }


    for key,value in section_patterns.items():

        if key in text:


            citation["document_part"] = value


            break


    # =================================================
    # 文档来源识别 V7
    # =================================================


    source_patterns = {


        "中华人民共和国公司法":
            "公司法正文",


        "公司登记管理条例":
            "公司登记管理条例",


        "中华人民共和国市场主体登记管理条例":
            "市场主体登记管理条例",


        "中华人民共和国企业破产法":
            "企业破产法",


        "上市公司信息披露管理办法":
            "上市公司规则",


        "中华人民共和国证券法":
            "证券法正文",

    }



    for key,value in source_patterns.items():


        if key in text:


            citation["source_document"] = key


            citation["document_part"] = value


            citation["law_name"] = key


            citation["citation_name"] = key


            citation["citation_type"] = "law"


            break



    # =================================================
    # 第一优先级：正式法律全称识别
    # =================================================


    for key,value in chapter_patterns.items():

        if key in text:

            citation["citation_name"] = value

            citation["law_name"] = value

            citation["document_part"] = key

            citation["citation_type"]="law"

            break
    

    law_patterns = {


        "中华人民共和国公司法":
        {
            "law_name":
                "中华人民共和国公司法",

            "document_part":
                "公司法正文",
        },


        "中华人民共和国民法典":
        {
            "law_name":
                "中华人民共和国民法典",

            "document_part":
                "民法典正文",
        },


        "中华人民共和国合同法":
        {
            "law_name":
                "中华人民共和国合同法",

            "document_part":
                "合同法正文",
        },


        "中华人民共和国证券法":
        {
            "law_name":
            "中华人民共和国证券法",

            "document_part":
                "证券法正文",
        },


        "中华人民共和国刑法":
        {
           "law_name":
               "中华人民共和国刑法",

            "document_part":
                "刑法正文",
        },


    }


              
                
    for law,info in law_patterns.items():
            

          
        if law in text:
        
    
            citation["citation_name"] = law
     
        
            citation["law_name"] = (
                info["law_name"]
            )
            
               
            citation["source_document"] = (
                Path(filename_text).stem
            )
     
        
            citation["document_part"] = (
                info["document_part"]
            )
            

            citation["citation_type"] = (
                "law"
            )
        
        
            break
    

    # =================================================
    # 法律文件类型识别
    # =================================================


    for key,info in law_type_patterns.items():


        if key in text:


            citation["document_subtype"] = (
                info["document_subtype"]
            )


            citation["law_level"] = (
                info["law_level"]
            )


            break



    # =================================================
    # 第二优先级：法律简称识别
    # =================================================


        law_alias_patterns = {
    

            "公司法":
                "中华人民共和国公司法",
    
        
            "民法典":
                "中华人民共和国民法典",
            
        
            "证券法":
                "中华人民共和国证券法",
    
                
            "刑法":
                "中华人民共和国刑法",
    
                
            "企业破产法":
                "中华人民共和国企业破产法",
            

            "合同法":
                "中华人民共和国合同法",
                
        }

        if not citation["citation_name"]:    
                
            for alias,name in law_alias_patterns.items():
                
            
                if alias in text:
                
                
                    citation["citation_name"] = name
                
                    citation["law_name"] = name
                
                    citation["source_document"] = Path(filename_text).stem
         
                    citation["document_part"] = (name.replace("中华人民共和国","")
                    )
       
                    citation["citation_type"] = "law"
         
                    break
                


    # =================================================
    # 第三优先级：文件名辅助识别
    # =================================================


    filename_patterns = {


        "中华人民共和国公司法":
            "中华人民共和国公司法",


        "中华人民共和国民法典":
            "中华人民共和国民法典",


        "中华人民共和国合同法":
            "中华人民共和国合同法",


        "中华人民共和国证券法":
            "中华人民共和国证券法",


        "中华人民共和国刑法":
            "中华人民共和国刑法",


    }



    if not citation["citation_name"]:


        for key,value in filename_patterns.items():

            if key in filename_text:


                citation["citation_name"] = value

                citation["law_name"] = value

                # 增加来源文件
                citation["source_document"] = (
                    Path(filename_text).stem
                )

                # 文件名无法确定章节
                citation["document_part"] = ""          

                citation["citation_type"] = "law"

                break



    # =================================================
    # 第四优先级：司法解释识别
    # =================================================

    if not citation["citation_name"]:


        judicial_patterns = [


            "最高人民法院关于适用",

            "最高人民法院关于审理",

            "最高人民法院关于执行",

            "若干问题的规定",

            "司法解释",

            "解释（一）",

            "解释（二）",

            "解释（三）",

            "解释（四）",

        ]


        for pattern in judicial_patterns:


            if pattern in text:


                citation["citation_name"] = (
                    "最高人民法院相关司法解释"
                )


                citation["law_name"] = (
                    "最高人民法院相关司法解释"
                )


                citation["citation_type"] = (
                    "judicial_interpretation"
                )


                break



    # =================================================
    # 条文号识别
    # =================================================

    articles = re.findall(

        r"第[一二三四五六七八九十百千万零〇0-9]+条",

        text

    )


    if articles:

        citation["citation_article"] = ",".join(

            list(dict.fromkeys(articles))

        )


    return citation


# =====================================================
# Metadata生成
# =====================================================

def detect_law_identity(
    filename,
    citation=None
):

    name = str(filename)



    # =====================
    # 正文识别优先
    # =====================

    if citation:


        if citation.get("law_name") in [

            "中华人民共和国公司法",
            "中华人民共和国民法典",
            "中华人民共和国合同法",
            "中华人民共和国证券法",
            "中华人民共和国刑法",

        ]:

            return "正式法律"



        if (
            citation.get("citation_type")
            ==
            "judicial_interpretation"
        ):

            return "司法解释"



    # =====================
    # 文件名辅助判断
    # =====================


    if (

        "司法解释" in name

        or

        "最高人民法院关于" in name

    ):

        return "司法解释"



    if (

        "理解与适用" in name

        or

        "条文理解" in name

    ):

        return "最高院理解与适用"



    if "评注" in name:

        return "专家评注"



    if (

        "问" in name

        or

        "教程" in name

        or

        "实务" in name

    ):

        return "专业参考"



    if (

        "案例" in name

        or

        "裁判" in name

    ):

        return "案例资料"



    return "其他资料"


def detect_law_level(identity):

    mapping = {

        "正式法律":
            "法律",

        "司法解释":
            "司法解释",

        "最高院理解与适用":
            "权威解释",

        "案例资料":
            "案例",

        "专家评注":
            "专业参考",

        "学者解读":
            "专业参考",

        "其他资料":
            "其他"

    }

    return mapping.get(identity,"其他")


def safe_list_to_text(value):

    if isinstance(value,list):

        return ",".join(value)

    return value or ""


def build_metadata(
    filepath,
    library_name,
    library_info,
    page_number,
    chunk_no,
    file_hash,
    chunk_text,
    article_no=""
):


    filepath = Path(filepath)

    filename = filepath.name

    title = filepath.stem

    citation = extract_citation(
        chunk_text,
        filename
    )

    articles = extract_articles(
        chunk_text
    )    
    
    if article_no:

        articles = [
            article_no
        ]

    law_identity = detect_law_identity(
        filename,
        citation
    )

    # 文档画像分类（纯 CPU 正则，带 memo：同一文件只分类一次）
    doc_meta = classify_document(filename, str(filepath), library_name)

    metadata = {

        "document_title":
            title,

        "citation_name":
            citation["citation_name"],

        "law_name":
            citation["law_name"],

        "source_document":
            citation.get(
                "source_document",
                ""
            ),


        "document_part":
            citation.get(
                "document_part",
                ""
            ),

        "document_subtype": citation.get("document_subtype",""),

        "citation_article":
            article_no or safe_list_to_text(articles),

        "citation_type":
            citation["citation_type"],
        
        "law_part":
            law_identity, 

        "filename":
            filename,


        "path":
            str(filepath),


        "page_number":
            str(page_number),


        "chunk_index":
            str(chunk_no),


        "file_hash":
            file_hash,


        "library":
            library_name,


        "type":
            "law",


        "year":
            library_info.get(
                "year",
                ""
            ),


        "source_priority":
            str(
                library_info.get(
                    "source_priority",
                    10
                )

             ),


        "created_time":
            "2026-08-05",


        # 新增字段

        "law_identity":
            law_identity,


        "law_level":
            detect_law_level(
                law_identity
            ),


        "effective_status":
            doc_meta["status"],


        # 文档画像（2026-09-02 元数据 schema 扩展）
        # doc_type: law/judicial_interpretation/regulation/departmental_rule/
        #           case/commentary/contract_template/practice_article/unknown
        # authority: 0-50 权威度基准（与 source_priority 量纲一致）
        # effective_date/version: 从文件名推断的时效信息（可为空）
        # domain: 公司/合同/劳动/资本市场…（可为空）

        "doc_type":
            doc_meta["doc_type"],


        "authority":
            doc_meta["authority"],


        "effective_date":
            doc_meta["effective_date"],


        "version":
            doc_meta["version"],


        "domain":
            doc_meta["domain"],


        "source_category":
            law_identity,


        "article_list":
            article_no
            if article_no
            else
            safe_list_to_text(
                extract_articles(chunk_text)
            ),


    }


    return metadata






# =====================================================
# 写入Chroma
# =====================================================


def write_collection(

    collection_name,

    documents,

    metadatas,

    ids

):


    if not documents:

        return 0



    collection = client.get_or_create_collection(

        name=collection_name

    )


    print(

        "准备写入chunks:",

        len(documents)

    )



    print(

        "生成向量..."

    )


    vectors = get_embedding_model().encode(

        documents,

        batch_size=32,

        show_progress_bar=False

    )



    total = len(documents)


    BATCH_SIZE = 5000



    written = 0



    for start in range(

        0,

        total,

        BATCH_SIZE

    ):


        end = min(

            start + BATCH_SIZE,

            total

        )


        print(

            f"写入Chroma: {start}-{end}"

        )

        print(
            metadatas[0]
        )

        collection.add(

            documents=

            documents[start:end],


            embeddings=[

                v.tolist()

                for v in vectors[start:end]

            ],


            metadatas=

            metadatas[start:end],


            ids=

            ids[start:end]

        )


        written += end-start



    print(

        "写入完成:",

        written

    )


    return written


# =====================================================
# 处理单个知识库
# =====================================================


def process_library(
    library_name,
    library_info
):


    print("\n")
    print("=" * 60)

    print(
        "处理知识库:",
        library_name
    )

    print(
        "类型:",
        library_info.get(
            "type"
        )
    )


    collection = client.get_or_create_collection(

        name=library_name

    )


    print(

        "当前chunks:",

        collection.count()

    )



    all_files = []



    for source in library_info.get(

        "sources",

        []

    ):


        path = source.get(
            "path"
        )


        source_priority = source.get(
            "priority",
            0
        )



        print(

            "扫描:",

            path

        )



        files = scan_source(
            path
        )


        if CONFIDENTIAL_DENIED_STATS["count"]:

            print(
                f"[保密守卫] 已拦截 {CONFIDENTIAL_DENIED_STATS['count']} 个客户材料文件"
                f"（示例: {CONFIDENTIAL_DENIED_STATS['samples'][:3]}）"
            )



        print(

            "发现:",

            len(files)

        )



        for f in files:


            all_files.append(

                {

                    "path":f,

                    "priority":
                        source_priority

                }

            )





    print(

        "\n文件总数:",

        len(all_files)

    )



    if args.limit:


        all_files = all_files[:args.limit]


        print(

            "限制处理:",

            args.limit

        )





    if args.dry_run:


        print(

            "\nDry Run模式"

        )


        for item in all_files[:10]:

            print(
                item["path"]
            )


        return







    documents = []

    metadatas = []

    ids = []



    processed = 0



    for item in tqdm(

        all_files,

        desc="处理文件"

    ):



        filepath = item["path"]



        file_hash = calculate_hash(
            filepath
        )



        if not file_hash:


            continue



        # 已处理跳过


        if file_hash in hash_index:


            continue




        pages = read_document(
            filepath
        )



        if not pages:


            continue




        chunk_no = 0



        for page_data in pages:



            text = page_data["text"]



            page_number = page_data.get(
                "page"
            )



            chunks = make_chunks(

                text,

                library_info.get(
                    "type",
                    ""
                )

            )



            # 法律切片绑定法条
            if library_info.get("type") == "law":

                chunks = bind_articles_to_chunks(
                    chunks
                )
            

            for chunk in chunks:

                # 统一chunk格式
                if isinstance(chunk, str):

                    chunk_text = chunk
                    chunk_article = ""

                else:
                    chunk_text = chunk.get("text", "")
                    chunk_article = chunk.get("article", "")


                doc_id = (


                    file_hash

                    +

                    "_"

                    +

                    str(chunk_no)

                )



                documents.append(
                    chunk_text
                )


                metadatas.append(

                    build_metadata(

                        filepath,

                        library_name,

                        library_info,

                        page_number,

                        chunk_no,

                        file_hash,
                        
                        chunk_text,
                      
                        chunk_article,

                        
                    ) 

                )



                ids.append(
                    doc_id
                )



                chunk_no += 1




        hash_index[file_hash] = {


            "path":

                filepath,


            "library":

                library_name,


            "time":

                datetime.datetime.now()
                .strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

        }


        save_hash_index()


        processed += 1




        # 批量写入


        if len(documents) >= 200:



            write_collection(

                library_name,

                documents,

                metadatas,

                ids

            )



            documents=[]

            metadatas=[]

            ids=[]





    # 最后剩余


    if documents:


        write_collection(

            library_name,

            documents,

            metadatas,

            ids

        )



    print(

        "\n完成文件:",

        processed

    )



    print(

        "当前chunks:",

        collection.count()

    )







# =====================================================
# 保存hash
# =====================================================


def save_hash_index():


    with open(

        HASH_INDEX_FILE,

        "w",

        encoding="utf-8"

    ) as f:


        json.dump(

            hash_index,

            f,

            ensure_ascii=False,

            indent=2

        )







# =====================================================
# 主程序
# =====================================================


def main():


    print("\n")

    print("="*60)

    print(

        "法律知识库构建系统 V6.5"

    )


    print(

        "启动时间:",

        datetime.datetime.now()

    )


    print("="*60)



    for name in selected_libraries:



        if args.dry_run:
            # dry-run 只扫描不写 Chroma：不加锁、不打快照（否则会挤掉真实写入的有效快照）
            process_library(
                name,
                libraries[name],
            )
        else:
            # P1-3：写入前先锁写；process_library 结束后（写成功）自动打快照，只留 N=3 份
            # （进程锁 + 快照兜底：防止 ingest + server 双进程同时写 SQLite + 坏了直接回滚）
            with chroma_write_lock():
                with snapshot_after_write(name, keep=3):
                    process_library(
                        name,
                        libraries[name],
                    )


    if not args.dry_run:


        save_hash_index()



    print("\n")

    print("="*60)

    print(

        "全部知识库构建完成"

    )


    print(

        "结束时间:",

        datetime.datetime.now()

    )

    print("="*60)







if __name__ == "__main__":


    main()
