"""
law-search 项目的唯一配置真相源（Single Source of Truth）。

⚠️  重要提示：本包是项目中 `import config` 时 Python 实际加载的模块（Python 导入优先级：package > file）。
            请 NEVER 在本包的上级目录（即 law-search/ 根目录）创建名为 `config.py` 的文件，
            否则会造成配置体系双轨混乱，维护者极易被误导。

子模块分工：
    - path_config.py     路径配置（Chroma、日志、知识库源等）
    - model_config.py    Embedding / Reranker / DEFAULT_LLM 模型配置
    - rag_config.py      检索与重排参数（TOP_K、权重、证据优先级）
    - system_config.py   系统版本、DEBUG 开关
    - prompt_config.py   System Prompt / 法律分析 / 诉讼文书 / 证据评估模板
    - logging_config.py  日志配置
    - llm_config.py      LLM 接入参数（LM Studio / DeepSeek API 多 Provider 预留）

使用方式：
    from config import LLM_MODEL, CHROMA_PATH, VERSION, ...
"""

from .path_config import *
from .model_config import *
from .agent_policy import *
from .rag_config import *
from .system_config import *
from .prompt_config import *
from .logging_config import *
from .llm_config import *
