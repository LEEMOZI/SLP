import os


# 项目根目录
BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)


# Chroma数据库
CHROMA_PATH = os.path.join(
    BASE_DIR,
    "chroma"
)


# 当前项目目录
PROJECT_DIR = BASE_DIR


# 配置目录
CONFIG_DIR = os.path.join(
    BASE_DIR,
    "config"
)


# 日志目录
LOG_DIR = os.path.join(
    BASE_DIR,
    "logs"
)


# 确保日志目录存在
os.makedirs(
    LOG_DIR,
    exist_ok=True
)


# 默认集合
COLLECTION_NAME = "law_library"


# 知识库来源清单（ingest.py 用）
# 说明：知识库源文档目录、library 分组等数据性配置在此文件中，
# 由 ingest.py 读取。config/ 是本项目所有配置的唯一目录。
PATHS_YAML = os.path.join(
    CONFIG_DIR,
    "paths.yaml"
)


# 文件哈希索引（ingest.py 增量去重用，属运行时数据，位于项目根目录）
HASH_INDEX_FILE = os.path.join(
    BASE_DIR,
    "file_hash_index.json"
)


# ==========================================================================
# 文件访问授权目录（read_document / contract_review 共用）
# ==========================================================================

# DSH Desktop 工作区：用户在对话中上传的文件会被拷贝到
# .dsh-uploads/session-<uuid>/<16位hash>-<原文件名>（原路径文件可能已不存在）
DSH_WORKSPACE_ROOT = "/Users/simon/Documents/1-SLP/DSH WorkSpace"
DSH_UPLOADS_ROOT = os.path.join(DSH_WORKSPACE_ROOT, ".dsh-uploads")

# 允许访问的根目录：工作台上传区 + law-search 项目区 + 用户工作区
ALLOWED_ROOTS: tuple = (
    "/Users/simon/Documents/1-Work",
    "/Users/simon/Documents/1-SLP/MCP/law-search",
    DSH_UPLOADS_ROOT,
)
