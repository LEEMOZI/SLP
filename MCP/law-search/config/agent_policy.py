"""Agent 任务统一策略：上下文预算、会话 TTL 与交付校验阈值。"""

# Mac Studio 上 Qwen3.8-27B 的加载上下文。
MODEL_CONTEXT_TOKENS = 131_072

# 路由分档：低于 soft 走本地；soft~hard 本地 128K 可接则本地，否则云端；
# 高于 hard 直接云端。
PROMPT_SOFT_LIMIT_TOKENS = 48_000
PROMPT_HARD_LIMIT_TOKENS = 100_000

# 超过该规模不再单请求硬塞，自动走 map-reduce 分段。
PROMPT_CHUNK_THRESHOLD_TOKENS = 400_000
MAX_SINGLE_PROMPT_TOKENS = 900_000

# map-reduce 分段策略：单段目标 token、合并要点再切阈值、map 并发数。
MAP_REDUCE_CHUNK_TOKENS = 48_000
MAP_REDUCE_REDUCE_INPUT_TOKENS = PROMPT_HARD_LIMIT_TOKENS
MAP_REDUCE_MAX_WORKERS = 4

# 输出预留，避免 prompt + output 撞上下文上限。
OUTPUT_RESERVE_TOKENS = 8_192

# 实测校准（2026-10-26 64K/128K A/B）：中文法律文本约 1.47 字符/token，
# 原估算按 2 字符/token，低估约 35%。统一乘该系数后再用于路由、超时与分段。
# 偏保守：宁可提前走云端/提高超时，也不要在本地 128K 上撞上下文。
TOKEN_ESTIMATE_SAFETY_FACTOR = 1.5

# 一个案件一个会话；TTL 到期后必须换新 case_id。
SESSION_TTL_HOURS = 24
MAX_TURNS_PER_CASE = 20

# 本地软超时按 prompt 规模分级（秒）。
# 注意：这里统计的是整个 Provider 请求（含生成），不是仅 TTFT。
LOCAL_SOFT_TIMEOUT_TIERS = (
    (16_000, 120),
    (64_000, 300),
    (131_072, 600),
)

# 正式交付必须提供的字段；缺失时只能标记 draft。
FORMAL_DELIVERY_REQUIRES_CASE_ID = True
FORMAL_DELIVERY_REQUIRES_SOURCE_DOCS = True
