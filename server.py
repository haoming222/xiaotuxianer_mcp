"""
================================================================================
 小兔鲜儿 MCP Server — 封装 7 个电商核心 API (HTTP SSE 模式)
================================================================================

 功能概述:
   将 Spring Boot 后端的商品搜索、详情、推荐、购物车、订单、支付、取消
   共 7 个 REST API 封装为 MCP (Model Context Protocol) Tool，
   让 Dify Agent 能通过标准 MCP 协议调用电商系统的全部操作。

 运行方式:
   conda activate python_mcp_server
   python server.py

 Dify 配置:
   MCP 类型选择 "SSE"，URL 填写: http://localhost:8000/sse

 架构链路:
   Dify Agent → MCP Protocol (SSE) → 本文件 → HTTP REST → Spring Boot 18080
"""

import json
import os
import time
from typing import Any

import httpx   # 异步 HTTP 客户端，用于调用 Spring Boot 后端 API
import uvicorn  # ASGI 服务器，承载 Starlette 应用
from mcp.server import Server
from mcp.server.sse import SseServerTransport  # MCP 的 SSE 传输层实现
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount, Route

# ==============================================================================
# 配置项 — 通过环境变量注入，方便 Docker/K8s 部署时动态修改
# ==============================================================================
SPRING_BOOT_URL = os.getenv("SPRING_BOOT_URL", "http://localhost:18080")
MCP_ACCOUNT = os.getenv("MCP_ACCOUNT", "zhm")
MCP_PASSWORD = os.getenv("MCP_PASSWORD", "123456")

# ==============================================================================
# JWT Token 缓存机制
# ==============================================================================
# 问题：7 个 Tool 中有 5 个需要 Authorization 请求头，
#       如果每次都重新登录获取 token，会产生大量冗余的 POST /login 请求
# 方案：首次调用时登录获取 token，缓存到内存中，
#       在 token 过期前 5 分钟自动刷新，避免过期边界问题

_token: str | None = None                   # 当前缓存的 JWT token
_token_expires_at: float = 0.0               # token 的过期时间戳（Unix 秒）


async def ensure_token(client: httpx.AsyncClient) -> str:
    """
    自动登录获取 JWT token，带缓存
    每次需要认证的工具调用前触发，确保始终有有效 token 可用
    """
    global _token, _token_expires_at
    now = time.time()

    # 如果 token 还没到"危险期"（过期前 5 分钟），直接复用缓存
    if _token and now < _token_expires_at - 300:
        return _token

    # 登录 → 获取新 token
    resp = await client.post(f"{SPRING_BOOT_URL}/login", json={
        "account": MCP_ACCOUNT,
        "password": MCP_PASSWORD,
    })
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        raise RuntimeError(f"登录失败: {body.get('msg')}")

    _token = body["data"]["token"]
    # JWT 实际有效期 30 分钟，但我们 25 分钟后就刷新
    # 留 5 分钟 buffer，防止 token 在两次调用之间恰好过期
    _token_expires_at = now + 25 * 60
    return _token


# ==============================================================================
# MCP Server 实例
# ==============================================================================
# "xiaotuxianer-mcp" 是此 MCP Server 的唯一名称标识，
# Dify 连接时会显示为工具提供方名称
app = Server("xiaotuxianer-mcp")


# ==============================================================================
# 工具注册 — 告知 LLM 有哪些 Tool 可用及每个 Tool 的参数 schema
# ==============================================================================
# Dify Agent 会根据这里的 description 和 inputSchema 来理解
# 每个工具的用途，并在合适时机调用

@app.list_tools()
async def list_tools() -> list[Tool]:
    """
    返回可用的 MCP Tool 列表
    此函数在 Dify 连接 MCP Server 时自动调用一次，用于工具发现
    """
    return [
        # ── Tool 1: 商品搜索 ──────────────────────────────────────────
        # 对应后端接口: GET /goods/search?keyword=&page=&pageSize=
        # 这是 5 步工作流的第一步：用户说"找羽绒服"，Agent 先调这个
        Tool(
            name="search_products",
            description="按关键词搜索商品，返回商品列表（含名称、ID、价格、主图）",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，如「羽绒服」「手机」",
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码，默认1",
                        "default": 1,
                    },
                    "pageSize": {
                        "type": "integer",
                        "description": "每页数量，默认10",
                        "default": 10,
                    },
                },
                "required": ["keyword"],
            },
        ),

        # ── Tool 2: 商品详情 ──────────────────────────────────────────
        # 对应后端接口: GET /goods/detail?id=
        # 工作流第二步：Agent 拿到搜索结果的商品 ID 后，调这个查看详情
        # 这是 LLM 做规格匹配的核心数据源：specs 数组 + skus 数组
        Tool(
            name="get_product_detail",
            description=(
                "获取单个商品完整信息："
                "名称、价格、品牌、全部规格(颜色/尺码)、全部SKU(库存+单价)、主图"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "goods_id": {
                        "type": "integer",
                        "description": "商品ID",
                    },
                },
                "required": ["goods_id"],
            },
        ),

        # ── Tool 3: 热门推荐 ──────────────────────────────────────────
        # 对应后端接口: GET /hot/{type}?page=&pageSize=
        # 工作流第三步：用热门推荐数据辅助决策
        # 注意：此接口不需要 token（HotController 无拦截器），
        # 所以 _get_trending_products 函数里没有调 ensure_token
        Tool(
            name="get_trending_products",
            description=(
                "获取热门推荐商品列表，支持4种类型: "
                "preference(特惠推荐)、inVogue(爆款推荐)、"
                "oneStop(一站买全)、new(新鲜好物)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["preference", "inVogue", "oneStop", "new"],
                        "description": "推荐类型",
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码，默认1",
                        "default": 1,
                    },
                    "pageSize": {
                        "type": "integer",
                        "description": "每页数量，默认10",
                        "default": 10,
                    },
                },
                "required": ["type"],
            },
        ),

        # ── Tool 4: 加入购物车 ────────────────────────────────────────
        # 对应后端接口: POST /cart/add
        # 需要传入 skuId（来自 get_product_detail 返回的 SKU 列表中某个 id）
        # 后端会在此步骤自动校验库存是否充足
        Tool(
            name="add_to_cart",
            description=(
                "加入购物车。需提供商品ID、SKU ID、数量、规格摘要"
                "（如「红色/XL」）。后端自动校验库存"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "goodsId": {
                        "type": "integer",
                        "description": "商品ID",
                    },
                    "skuId": {
                        "type": "string",
                        "description": "SKU ID（来自 get_product_detail 返回的 skus 列表中某个 skuId）",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "购买数量，默认1",
                        "default": 1,
                    },
                    "specSummary": {
                        "type": "string",
                        "description": "规格摘要描述，如「红色 / XL」，用于前端展示",
                    },
                },
                "required": ["goodsId", "skuId", "specSummary"],
            },
        ),

        # ── Tool 5: 创建订单 ──────────────────────────────────────────
        # 对应后端接口: POST /order/create
        # 工作流第四步：选定规格后直接下单
        # 订单创建后会写入 Redis 键，5 分钟后过期触发自动取消（兜底机制）
        Tool(
            name="create_order",
            description=(
                "创建订单（下单）。传入商品ID、SKU ID、数量、规格摘要。"
                "返回订单详情含总价和ID。"
                "订单创建后5分钟内未支付将自动取消（Redis兜底）"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "goodsId": {
                        "type": "integer",
                        "description": "商品ID",
                    },
                    "skuId": {
                        "type": "string",
                        "description": "SKU ID",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "购买数量，默认1",
                        "default": 1,
                    },
                    "specSummary": {
                        "type": "string",
                        "description": "规格摘要，如「红色 / XL」",
                    },
                },
                "required": ["goodsId", "skuId", "specSummary"],
            },
        ),

        # ── Tool 6: 支付订单 ──────────────────────────────────────────
        # 对应后端接口: PUT /order/pay/{id}
        # 工作流第五步：确认支付
        # 底层 SQL: UPDATE ... WHERE id=? AND account=? AND status=0
        # 三重校验防止越权支付或重复扣款
        Tool(
            name="pay_order",
            description="支付订单，将订单状态从「待支付」变为「已支付」。需提供订单ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "订单ID（create_order 返回的 orderId）",
                    },
                },
                "required": ["order_id"],
            },
        ),

        # ── Tool 7: 取消订单 ──────────────────────────────────────────
        # 对应后端接口: PUT /order/cancel/{id}
        # 与自动取消（Redis 过期监听）互补，Agent 也可主动取消订单
        Tool(
            name="cancel_order",
            description="手动取消订单，将订单状态从「待支付」变为「已取消」。需提供订单ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "订单ID",
                    },
                },
                "required": ["order_id"],
            },
        ),
    ]


# ==============================================================================
# 工具调用分发 — Dify Agent 调用 Tool 时的入口函数
# ==============================================================================
# 这是 MCP 协议的核心方法：Agent 传入 tool 名称 + 参数，
# 我们需要执行对应的业务逻辑并返回结果

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """
    根据 tool 名称路由到对应的处理函数，统一错误处理包装
    """
    # 每个 HTTP 调用使用独立的 AsyncClient 实例
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # ── 路由分发 ──
            if name == "search_products":
                result = await _search_products(client, arguments)
            elif name == "get_product_detail":
                result = await _get_product_detail(client, arguments)
            elif name == "get_trending_products":
                result = await _get_trending_products(client, arguments)
            elif name == "add_to_cart":
                result = await _add_to_cart(client, arguments)
            elif name == "create_order":
                result = await _create_order(client, arguments)
            elif name == "pay_order":
                result = await _pay_order(client, arguments)
            elif name == "cancel_order":
                result = await _cancel_order(client, arguments)
            else:
                # 未知工具名，返回错误提示
                return [TextContent(type="text", text=f"未知工具: {name}")]

            # 将 Python dict 转为格式化 JSON 字符串返回给 LLM
            # ensure_ascii=False 保证中文不转义
            # indent=2 让 LLM 更容易"读"懂 JSON 结构
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, indent=2),
                )
            ]

        except httpx.HTTPStatusError as e:
            # 后端返回了非 2xx 状态码（如 404、500）
            return [
                TextContent(
                    type="text",
                    text=f"HTTP 错误 {e.response.status_code}: {e.response.text}",
                )
            ]
        except Exception as e:
            # 其他未预期错误（网络超时、DNS 解析失败等）
            return [TextContent(type="text", text=f"调用失败: {str(e)}")]


# ==============================================================================
# 各 Tool 的具体实现
# ==============================================================================
# 每个函数负责：
#   1. 构造请求参数
#   2. 调用 Spring Boot 后端 HTTP API
#   3. 解析响应
#   4. 精简数据（去掉 LLM 不需要的字段，节省 token 消耗）
#   5. 返回结构化的 dict/json


async def _search_products(client: httpx.AsyncClient, args: dict) -> dict:
    """
    商品搜索 — 对应 GET /goods/search
    按关键词模糊匹配商品名称，返回分页结果
    返回字段裁剪：只保留 id/name/price/description/salesCount，
    不返回完整的详情图/规格等（减少 LLM context 消耗）
    """
    keyword = args["keyword"]
    page = args.get("page", 1)
    page_size = args.get("pageSize", 10)

    token = await ensure_token(client)  # 自动登录获取 JWT

    resp = await client.get(
        f"{SPRING_BOOT_URL}/goods/search",
        params={"keyword": keyword, "page": page, "pageSize": page_size},
        headers={"Authorization": token},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"error": body.get("msg", "搜索失败")}

    data = body.get("data", {})
    return {
        "total": data.get("total", 0),
        "page": data.get("pageNum", page),
        "products": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "price": p.get("price"),
                "oldPrice": p.get("oldPrice"),
                "description": p.get("description"),
                "salesCount": p.get("salesCount"),
            }
            for p in data.get("list", [])
        ],
    }


async def _get_product_detail(client: httpx.AsyncClient, args: dict) -> dict:
    """
    商品详情 — 对应 GET /goods/detail?id=
    这是 LLM 最关键的数据源，返回商品的完整信息：
      - 基本信息：名称、价格、销量、评价数
      - 规格维度：颜色有哪些、尺码有哪些（specs 数组）
      - SKU 详情：每个规格组合对应的库存、单价、图片（skus 数组）
      - 相似商品：用于推荐对比
    后端会 join 10+ 张表，这里帮 LLM 把数据重组为更容易理解的结构
    """
    goods_id = args["goods_id"]

    token = await ensure_token(client)

    resp = await client.get(
        f"{SPRING_BOOT_URL}/goods/detail",
        params={"id": goods_id},
        headers={"Authorization": token},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"error": body.get("msg", "查询失败")}

    data = body.get("data", {})

    # ── 重组 SKU 数据 ──
    # 将 specs 数组扁平化为可读字符串，如 "颜色: 红色, 尺码: XL"
    # LLM 可以直接读懂，不需要再解析嵌套数组
    skus = []
    for sku in data.get("skus", []):
        spec_names = ", ".join(
            f"{s.get('name')}: {s.get('valueName')}" for s in sku.get("specs", [])
        )
        skus.append({
            "skuId": sku.get("id"),           # SKU 主键，后续加购/下单需要传
            "specs": spec_names,               # 规格描述，LLM 用于匹配
            "price": sku.get("price"),         # 该规格组合的价格
            "inventory": sku.get("inventory"), # 库存数量，用于判断能否购买
            "picture": sku.get("picture"),     # 该规格对应的图片
        })

    # ── 重组规格维度 ──
    # 从 specs 数组中提取"有哪些可选维度"
    # 如: [{name: "颜色", values: ["红色", "蓝色"]}, {name: "尺码", values: ["S", "M", "XL"]}]
    specs_info = []
    for spec in data.get("specs", []):
        specs_info.append({
            "name": spec.get("name"),  # 维度名称，如"颜色"
            "values": [v.get("name") for v in spec.get("values", [])],  # 可选值列表
        })

    # ── 精简相似商品 ──
    # 只保留 id/name/price，不返回完整详情
    similar = [
        {"id": sp.get("id"), "name": sp.get("name"), "price": sp.get("price")}
        for sp in data.get("similarProducts", [])
    ]

    return {
        # 基本信息
        "id": data.get("id"),
        "name": data.get("name"),
        "price": data.get("price"),
        "oldPrice": data.get("oldPrice"),
        "description": data.get("description"),
        # 统计数据
        "salesCount": data.get("salesCount"),
        "commentCount": data.get("commentCount"),
        "collectCount": data.get("collectCount"),
        # 品牌 & 主图
        "brand": data.get("brand"),
        "mainPictures": data.get("mainPictures", []),
        # 规格 & SKU（LLM 决策的核心）
        "specs": specs_info,
        "skus": skus,
        # 相似商品推荐
        "similarProducts": similar,
    }


async def _get_trending_products(client: httpx.AsyncClient, args: dict) -> dict:
    """
    热门推荐 — 对应 GET /hot/{type}
    支持 4 种推荐类型：
      preference → 特惠推荐
      inVogue    → 爆款推荐
      oneStop    → 一站买全
      new        → 新鲜好物
    注意：此接口无需 token（HotController 路径不在拦截器范围）
    """
    hot_type = args["type"]
    page = args.get("page", 1)
    page_size = args.get("pageSize", 10)

    # 注意：这里没有调 ensure_token，因为 /hot/** 不需要认证
    resp = await client.get(
        f"{SPRING_BOOT_URL}/hot/{hot_type}",
        params={"page": page, "pageSize": page_size},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"error": body.get("msg", "查询失败")}

    data = body.get("data", {})
    # 扁平化：把 subtypes → goodsItems.list 展平为单一商品列表
    items = []
    for st in data.get("subtypes", []):
        for item in st.get("goodsItems", {}).get("list", []):
            items.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "price": item.get("price"),
                "picture": item.get("picture"),
                "description": item.get("description"),
            })

    type_names = {
        "preference": "特惠推荐",
        "inVogue": "爆款推荐",
        "oneStop": "一站买全",
        "new": "新鲜好物",
    }

    return {
        "type": type_names.get(hot_type, hot_type),
        "title": data.get("title", ""),
        "bannerPicture": data.get("bannerPicture", ""),
        "products": items,
    }


async def _add_to_cart(client: httpx.AsyncClient, args: dict) -> dict:
    """
    加入购物车 — 对应 POST /cart/add
    后端会校验：
      1. skuId 是否属于该商品
      2. 购买数量是否 ≤ 当前库存
      3. goodsId 不为空
    校验失败会返回具体错误信息
    """
    token = await ensure_token(client)

    payload = {
        "goodsId": args["goodsId"],
        "skuId": args["skuId"],
        "quantity": args.get("quantity", 1),
        "specSummary": args["specSummary"],
    }

    resp = await client.post(
        f"{SPRING_BOOT_URL}/cart/add",
        json=payload,
        headers={"Authorization": token},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"success": False, "error": body.get("msg", "加入购物车失败")}

    return {"success": True, "message": "已成功加入购物车"}


async def _create_order(client: httpx.AsyncClient, args: dict) -> dict:
    """
    创建订单 — 对应 POST /order/create
    后端处理流程：
      1. 参数校验（goodsId 不为空、quantity ≥ 1）
      2. 查询商品和 SKU 信息，计算总价
      3. 校验库存是否充足
      4. INSERT 订单记录（状态=0 待支付）
      5. 写入 Redis: order:auto_cancel:{orderId}，TTL=5分钟
      6. Redis 键过期后由 RedisKeyExpirationListener 自动将状态改为"已取消"
    返回订单详情，建议 LLM 提示用户 5 分钟内完成支付
    """
    token = await ensure_token(client)

    payload = {
        "goodsId": args["goodsId"],
        "skuId": args["skuId"],
        "quantity": args.get("quantity", 1),
        "specSummary": args["specSummary"],
    }

    resp = await client.post(
        f"{SPRING_BOOT_URL}/order/create",
        json=payload,
        headers={"Authorization": token},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"success": False, "error": body.get("msg", "下单失败")}

    order = body.get("data", {})

    return {
        "success": True,
        "message": "订单创建成功，请在5分钟内完成支付，超时将自动取消",
        "order": {
            "orderId": order.get("id"),
            "goodsName": order.get("goodsName"),
            "specSummary": order.get("specSummary"),
            "quantity": order.get("quantity"),
            "price": str(order.get("price")),           # BigDecimal → String
            "totalPrice": str(order.get("totalPrice")), # 总价 = 单价 × 数量
            "status": "待支付",
        },
    }


async def _pay_order(client: httpx.AsyncClient, args: dict) -> dict:
    """
    支付订单 — 对应 PUT /order/pay/{id}
    后端 SQL: UPDATE order_info SET status=1
             WHERE id=? AND account=? AND status=0
    三重校验：
      1. id 匹配 —— 订单存在
      2. account 匹配 —— 防止越权支付他人订单
      3. status=0 —— 防止重复支付或支付已取消的订单
    如果 affected rows = 0，说明上述条件之一不满足，返回失败
    """
    token = await ensure_token(client)
    order_id = args["order_id"]

    resp = await client.put(
        f"{SPRING_BOOT_URL}/order/pay/{order_id}",
        headers={"Authorization": token},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"success": False, "error": body.get("msg", "支付失败")}

    return {"success": True, "message": f"订单 {order_id} 已支付成功"}


async def _cancel_order(client: httpx.AsyncClient, args: dict) -> dict:
    """
    手动取消订单 — 对应 PUT /order/cancel/{id}
    与上面的支付逻辑对称，条件相同，只是 status 目标值不同：
      status 0(待支付) → 2(已取消)
    注意：
      - 如果订单已被 Redis 自动取消，此操作会返回失败（status 已经是 2）
      - 如果订单已支付（status=1），取消也会失败
    """
    token = await ensure_token(client)
    order_id = args["order_id"]

    resp = await client.put(
        f"{SPRING_BOOT_URL}/order/cancel/{order_id}",
        headers={"Authorization": token},
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("code") != 200:
        return {"success": False, "error": body.get("msg", "取消失败")}

    return {"success": True, "message": f"订单 {order_id} 已取消"}


# ==============================================================================
# HTTP SSE 启动入口
# ==============================================================================
# MCP 协议支持两种传输方式：
#   stdio — 进程间标准输入输出（适合本地命令行调用）
#   SSE   — Server-Sent Events over HTTP（适合 Dify 等平台通过 URL 连接）
# 这里使用 SSE 模式，Dify 通过 http://localhost:8000/sse 连接

# 可被环境变量覆盖的 HTTP 配置
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# SseServerTransport 是 MCP 官方提供的 SSE 传输实现
# "/messages/" 路径用于 POST 接收客户端发来的初始化消息和 tool 调用请求
sse = SseServerTransport("/messages/")


async def handle_sse(request):
    """
    处理 SSE 连接请求
    Dify Agent 向 /sse 发起 GET 请求后，此函数建立长连接，
    通过 SSE 通道接收 tool 调用并进行响应
    """
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        # 将 SSE 通道包装为 MCP Server 所需的 read/write stream
        await app.run(
            read_stream, write_stream, app.create_initialization_options()
        )


def main():
    """
    构建 Starlette 应用并启动 uvicorn

    注册两个路由：
      GET  /sse         → SSE 连接端点（Dify 连接此 URL）
      POST /messages/   → MCP 消息端点（tool 调用通过此路径）
    """
    starlette_app = Starlette(
        routes=[
            # SSE 长连接端点 — Dify 配置中填写的 URL
            Route("/sse", endpoint=handle_sse),

            # MCP 消息处理 — 注意使用 Mount 而非 Route
            # 因为 sse.handle_post_message 是 ASGI app (scope, receive, send)
            # 不是普通的 HTTP request handler
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )
    uvicorn.run(starlette_app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    print(f"  小兔鲜儿 MCP Server 启动在 http://{MCP_HOST}:{MCP_PORT}")
    print(f"  SSE 端点: http://localhost:{MCP_PORT}/sse")
    print(f"  后端地址: {SPRING_BOOT_URL}")
    main()