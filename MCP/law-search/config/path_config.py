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

# DSH Desktop 旧上传区（历史兼容；当前工作区已迁移到 2-WorkSpace）
# 用户在对话中上传的文件可能被拷贝到
# .dsh-uploads/session-<uuid>/<16位hash>-<原文件名>（原路径文件可能已不存在）
DSH_WORKSPACE_ROOT = "/Users/simon/Documents/1-SLP/DSH WorkSpace"
DSH_UPLOADS_ROOT = os.path.join(DSH_WORKSPACE_ROOT, ".dsh-uploads")

# 实际工作区与 Cherry Studio 上传目录
WORKSPACE_ROOT = "/Users/simon/Documents/2-WorkSpace"
CHERRY_FILES_ROOT = os.path.expanduser(
    "~/Library/Application Support/CherryStudio/Data/Files"
)

# 环境变量可追加授权目录（os.pathsep 分隔），便于不同机器部署
_EXTRA_ALLOWED_ROOTS = tuple(
    os.path.expanduser(p.strip())
    for p in os.environ.get("LAW_SEARCH_ALLOWED_ROOTS", "").split(os.pathsep)
    if p.strip()
)

# 允许访问的根目录：实际工作区 + Cherry 上传区 + law-search 项目区 + DSH 旧上传区
ALLOWED_ROOTS: tuple = (
    WORKSPACE_ROOT,
    CHERRY_FILES_ROOT,
    "/Users/simon/Documents/1-SLP/MCP/law-search",
    DSH_UPLOADS_ROOT,
) + _EXTRA_ALLOWED_ROOTS


def validate_allowed_roots() -> tuple:
    """返回不存在或不可读的授权目录，供服务启动时告警。"""
    return tuple(
        root
        for root in ALLOWED_ROOTS
        if not os.path.isdir(root) or not os.access(root, os.R_OK)
    )
