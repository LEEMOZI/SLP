"""
Evidence Context Builder

负责将 Evidence Pack 转换为
LLM 可理解的法律证据上下文。

输入:
    evidence_pack

输出:
    str
"""


def build_evidence_context(
    evidence_pack,
    max_items=5
):
    """
    构建法律分析上下文

    参数:
        evidence_pack:
            build_evidence_pack() 输出

        max_items:
            最大引用证据数量

    返回:
        str
    """

    if not evidence_pack:
        return ""


    contexts = []


    for evidence in evidence_pack[:max_items]:

        evidence_id = evidence.get(
            "evidence_id",
            ""
        )

        title = evidence.get(
            "title",
            ""
        )

        authority = evidence.get(
            "authority",
            ""
        )

        article = evidence.get(
            "article",
            ""
        )

        page = evidence.get(
            "page",
            ""
        )

        source_type = evidence.get(
            "source_type",
            ""
        )        

        role = evidence.get(
            "role",
            ""
        )

        category = evidence.get(
            "category",
            ""
        )

        content = evidence.get(
            "content",
            ""
        )

        block = f"""
【证据{evidence_id}】

来源：
{title}

来源类型：
{source_type}

权威等级：
{authority}

条文位置：
{article}

页码：
{page}

证据类型：
{category}

证据角色：
{role}

内容：
{content}

--------------------
"""


        contexts.append(
            block.strip()
        )


    return "\n\n".join(
        contexts
    )
