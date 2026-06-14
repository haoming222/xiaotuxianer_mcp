import json
import os
import time
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent


# ==========================================================
# 基础配置
# ==========================================================
# Spring Boot 后端地址
# 本地默认是 http://localhost:18080
# 如果 MCP 跑在 Docker 里，可能要改成宿主机 IP 或容器网络地址
SPRING_BOOT_URL = os.getenv("SPRING_BOOT_URL", "http://localhost:18080")

# MCP 调用 Spring Boot 登录接口时使用的账号密码
MCP_ACCOUNT = os.getenv("MCP_ACCOUNT", "zhm")
MCP_PASSWORD = os.getenv("MCP_PASSWORD", "123456")

# MCP Server 监听地址
# 0.0.0.0 表示允许外部访问，方便 Dify Docker 容器连接
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))


# ==========================================================
# JWT Token 缓存
# ==========================================================
# 多数 Spring Boot 接口需要 Authorization Token。
#
# 如果每次 Tool 调用都重新登录，会产生大量重复请求：
#
#   Dify Agent
#       ↓
#   MCP Tool
#       ↓
#   POST /login
#       ↓
#   调用业务接口
#
# 所以这里做内存缓存：
#
#   第一次调用：登录获取 Token
#   后续调用：复用 Token
#   接近过期：自动刷新 Token
# ==========================================================
_token: str | None = None
_token_expires_at: float = 0.0


async def ensure_token(client: httpx.AsyncClient) -> str:
    """
    获取一个可用的 JWT Token。

    设计目的：
    1. 避免每次调用工具都重新登录
    2. 减少 Spring Boot 登录接口压力
    3. 避免 Token 临近过期导致请求失败

    当前策略：
    - Token 认为有效期约 30 分钟
    - 代码中按照 25 分钟刷新
    - 预留 5 分钟安全缓冲
    """
    global _token, _token_expires_at

    now = time.time()

    # Token 存在且还没进入过期危险期，直接复用
    if _token and now < _token_expires_at - 300:
        return _token

    # Token 不存在或即将过期，重新登录
    resp = await client.post(
        f"{SPRING_BOOT_URL}/login",
        json={
            "account": MCP_ACCOUNT,
            "password": MCP_PASSWORD,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        raise RuntimeError(f"登录失败: {body.get('msg')}")

    _token = body["data"]["token"]
    _token_expires_at = now + 25 * 60

    return _token


# ==========================================================
# MCP Server 实例
# ==========================================================
# 这个名字会作为 MCP 服务名称展示给 Dify。
#
# 整体链路：
#
#   Dify Agent
#       ↓
#   MCP Protocol / SSE
#       ↓
#   xiaotuxianer-mcp
#       ↓
#   Spring Boot REST API
# ==========================================================
app = Server("xiaotuxianer-mcp")


# ==========================================================
# 工具发现接口
# ==========================================================
# Dify 第一次连接 MCP Server 时，会调用 list_tools。
#
# 这里返回所有可用工具：
# 1. 工具名称
# 2. 工具描述
# 3. 参数 schema
#
# Dify Agent 会根据这些描述判断什么时候调用哪个工具。
# ==========================================================
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_products",
            description="按关键词搜索商品，返回商品列表（含名称、ID、价格、销量）",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，如：羽绒服、手机、书包",
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码，默认 1",
                        "default": 1,
                    },
                    "pageSize": {
                        "type": "integer",
                        "description": "每页数量，默认 10",
                        "default": 10,
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="get_product_detail",
            description="获取单个商品完整信息，包括规格、SKU、库存、价格、相似商品",
            inputSchema={
                "type": "object",
                "properties": {
                    "goods_id": {
                        "type": "integer",
                        "description": "商品 ID",
                    },
                },
                "required": ["goods_id"],
            },
        ),
        Tool(
            name="get_trending_products",
            description="获取热门推荐商品，支持 preference、inVogue、oneStop、new 四种类型",
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
                        "description": "页码，默认 1",
                        "default": 1,
                    },
                    "pageSize": {
                        "type": "integer",
                        "description": "每页数量，默认 10",
                        "default": 10,
                    },
                },
                "required": ["type"],
            },
        ),
        Tool(
            name="add_to_cart",
            description="加入购物车，需要商品 ID、SKU ID、数量、规格摘要",
            inputSchema={
                "type": "object",
                "properties": {
                    "goodsId": {
                        "type": "integer",
                        "description": "商品 ID",
                    },
                    "skuId": {
                        "type": "string",
                        "description": "SKU ID，来自商品详情接口返回的 skus",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "购买数量，默认 1",
                        "default": 1,
                    },
                    "specSummary": {
                        "type": "string",
                        "description": "规格摘要，如：红色 / XL",
                    },
                },
                "required": ["goodsId", "skuId", "specSummary"],
            },
        ),
        Tool(
            name="create_order",
            description="创建订单，需要商品 ID、SKU ID、数量、规格摘要，返回订单信息",
            inputSchema={
                "type": "object",
                "properties": {
                    "goodsId": {
                        "type": "integer",
                        "description": "商品 ID",
                    },
                    "skuId": {
                        "type": "string",
                        "description": "SKU ID",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "购买数量，默认 1",
                        "default": 1,
                    },
                    "specSummary": {
                        "type": "string",
                        "description": "规格摘要，如：红色 / XL",
                    },
                },
                "required": ["goodsId", "skuId", "specSummary"],
            },
        ),
        Tool(
            name="pay_order",
            description="支付订单，将订单状态从待支付变为已支付",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "订单 ID",
                    },
                },
                "required": ["order_id"],
            },
        ),
        Tool(
            name="cancel_order",
            description="取消订单，将订单状态从待支付变为已取消",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "订单 ID",
                    },
                },
                "required": ["order_id"],
            },
        ),
    ]


# ==========================================================
# 工具调用入口
# ==========================================================
# 当 Dify Agent 决定调用某个工具时，会进入这里。
#
# 例如：
#
#   用户：帮我找一件羽绒服
#
#   Dify Agent 判断应该调用：
#   search_products({"keyword": "羽绒服"})
#
#   MCP Server 接收到后：
#   call_tool("search_products", {"keyword": "羽绒服"})
#
# 然后这里再路由到 _search_products 函数。
# ==========================================================
@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
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
                return [TextContent(type="text", text=f"未知工具: {name}")]

            # MCP Tool 返回的是文本内容
            # 这里把 Python dict 转成 JSON 字符串，方便大模型理解
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, indent=2),
                )
            ]

        except httpx.HTTPStatusError as e:
            # Spring Boot 返回非 2xx 状态码时进入这里
            return [
                TextContent(
                    type="text",
                    text=f"HTTP 错误 {e.response.status_code}: {e.response.text}",
                )
            ]
        except Exception as e:
            # 网络异常、JSON 解析异常、业务异常等进入这里
            return [
                TextContent(
                    type="text",
                    text=f"调用失败: {str(e)}",
                )
            ]


# ==========================================================
# Tool 1：商品搜索
# ==========================================================
async def _search_products(client: httpx.AsyncClient, args: dict) -> dict:
    """
    商品搜索。

    对应 Spring Boot 接口：

        GET /goods/search

    适合场景：

        用户：帮我找羽绒服
        用户：搜索一下手机
        用户：有没有书包

    注意：
    这里不返回完整商品详情，只返回精简信息。
    这样可以减少 LLM 上下文消耗，提高 Agent 判断效率。
    """
    keyword = args["keyword"]
    page = args.get("page", 1)
    page_size = args.get("pageSize", 10)

    token = await ensure_token(client)

    resp = await client.get(
        f"{SPRING_BOOT_URL}/goods/search",
        params={
            "keyword": keyword,
            "page": page,
            "pageSize": page_size,
        },
        headers={
            "Authorization": token,
        },
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


# ==========================================================
# Tool 2：商品详情
# ==========================================================
async def _get_product_detail(client: httpx.AsyncClient, args: dict) -> dict:
    """
    获取商品详情。

    对应 Spring Boot 接口：

        GET /goods/detail?id=xxx

    这是 Agent 决策中非常关键的一步。

    因为下单不能只知道商品 ID，
    还必须知道具体 SKU ID。

    例如：

        用户：我要红色 XL 的那件

    Agent 需要从 skus 里找到：
        颜色 = 红色
        尺码 = XL
        库存 > 0

    然后把对应 skuId 传给下单接口。
    """
    goods_id = args["goods_id"]

    token = await ensure_token(client)

    resp = await client.get(
        f"{SPRING_BOOT_URL}/goods/detail",
        params={"id": goods_id},
        headers={
            "Authorization": token,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        return {"error": body.get("msg", "查询失败")}

    data = body.get("data", {})

    # 把 SKU 中的规格数组转换成人类更容易读懂的字符串
    # 例如：
    #   [{"name": "颜色", "valueName": "红色"}, {"name": "尺码", "valueName": "XL"}]
    # 转成：
    #   "颜色: 红色, 尺码: XL"
    skus = []
    for sku in data.get("skus", []):
        spec_names = ", ".join(
            f"{s.get('name')}: {s.get('valueName')}"
            for s in sku.get("specs", [])
        )

        skus.append(
            {
                "skuId": sku.get("id"),
                "specs": spec_names,
                "price": sku.get("price"),
                "inventory": sku.get("inventory"),
                "picture": sku.get("picture"),
            }
        )

    # 提取商品有哪些规格维度
    # 例如：
    #   颜色：红色、黑色、蓝色
    #   尺码：S、M、L、XL
    specs_info = []
    for spec in data.get("specs", []):
        specs_info.append(
            {
                "name": spec.get("name"),
                "values": [
                    v.get("name")
                    for v in spec.get("values", [])
                ],
            }
        )

    # 相似商品只保留核心字段，避免上下文过大
    similar = [
        {
            "id": sp.get("id"),
            "name": sp.get("name"),
            "price": sp.get("price"),
        }
        for sp in data.get("similarProducts", [])
    ]

    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "price": data.get("price"),
        "oldPrice": data.get("oldPrice"),
        "description": data.get("description"),
        "salesCount": data.get("salesCount"),
        "commentCount": data.get("commentCount"),
        "collectCount": data.get("collectCount"),
        "brand": data.get("brand"),
        "mainPictures": data.get("mainPictures", []),
        "specs": specs_info,
        "skus": skus,
        "similarProducts": similar,
    }


# ==========================================================
# Tool 3：热门推荐
# ==========================================================
async def _get_trending_products(client: httpx.AsyncClient, args: dict) -> dict:
    """
    获取热门推荐商品。

    对应 Spring Boot 接口：

        GET /hot/{type}

    支持四种推荐类型：

        preference  特惠推荐
        inVogue     爆款推荐
        oneStop     一站买全
        new         新鲜好物

    这个接口一般用于：
    1. 用户不知道买什么
    2. Agent 做主动推荐
    3. 搜索结果为空时兜底推荐
    """
    hot_type = args["type"]
    page = args.get("page", 1)
    page_size = args.get("pageSize", 10)

    # 假设 /hot/** 不需要登录
    resp = await client.get(
        f"{SPRING_BOOT_URL}/hot/{hot_type}",
        params={
            "page": page,
            "pageSize": page_size,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        return {"error": body.get("msg", "查询失败")}

    data = body.get("data", {})

    # 后端返回可能是 subtypes 嵌套结构
    # 这里将其拍平成一个商品列表，方便 LLM 阅读
    items = []
    for st in data.get("subtypes", []):
        goods_items = st.get("goodsItems", {})
        for item in goods_items.get("list", []):
            items.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "price": item.get("price"),
                    "picture": item.get("picture"),
                    "description": item.get("description"),
                }
            )

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


# ==========================================================
# Tool 4：加入购物车
# ==========================================================
async def _add_to_cart(client: httpx.AsyncClient, args: dict) -> dict:
    """
    加入购物车。

    对应 Spring Boot 接口：

        POST /cart/add

    必须传入：
    1. goodsId
    2. skuId
    3. quantity
    4. specSummary

    注意：
    skuId 一定要来自 get_product_detail 返回的 skus。
    因为同一个商品可能有多个颜色和尺码组合。
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
        headers={
            "Authorization": token,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        return {
            "success": False,
            "error": body.get("msg", "加入购物车失败"),
        }

    return {
        "success": True,
        "message": "已成功加入购物车",
    }


# ==========================================================
# Tool 5：创建订单
# ==========================================================
async def _create_order(client: httpx.AsyncClient, args: dict) -> dict:
    """
    创建订单。

    对应 Spring Boot 接口：

        POST /order/create

    典型流程：

        用户确认商品和规格
              ↓
        Agent 找到 skuId
              ↓
        调用 create_order
              ↓
        Spring Boot 校验库存
              ↓
        创建待支付订单
              ↓
        返回 orderId

    如果你的后端做了 Redis 自动取消订单，
    可以在这里提示用户 5 分钟内完成支付。
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
        headers={
            "Authorization": token,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        return {
            "success": False,
            "error": body.get("msg", "下单失败"),
        }

    order = body.get("data", {})

    return {
        "success": True,
        "message": "订单创建成功，请在5分钟内完成支付，超时将自动取消",
        "order": {
            "orderId": order.get("id"),
            "goodsName": order.get("goodsName"),
            "specSummary": order.get("specSummary"),
            "quantity": order.get("quantity"),
            "price": str(order.get("price")),
            "totalPrice": str(order.get("totalPrice")),
            "status": "待支付",
        },
    }


# ==========================================================
# Tool 6：支付订单
# ==========================================================
async def _pay_order(client: httpx.AsyncClient, args: dict) -> dict:
    """
    支付订单。

    对应 Spring Boot 接口：

        PUT /order/pay/{id}

    后端一般应该校验：
    1. 订单是否存在
    2. 订单是否属于当前用户
    3. 订单状态是否仍为待支付
    4. 是否已经超时取消
    """
    token = await ensure_token(client)

    order_id = args["order_id"]

    resp = await client.put(
        f"{SPRING_BOOT_URL}/order/pay/{order_id}",
        headers={
            "Authorization": token,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        return {
            "success": False,
            "error": body.get("msg", "支付失败"),
        }

    return {
        "success": True,
        "message": f"订单 {order_id} 已支付成功",
    }


# ==========================================================
# Tool 7：取消订单
# ==========================================================
async def _cancel_order(client: httpx.AsyncClient, args: dict) -> dict:
    """
    取消订单。

    对应 Spring Boot 接口：

        PUT /order/cancel/{id}

    可用于：
    1. 用户主动取消
    2. Agent 根据用户意图取消
    3. 与 Redis 自动取消机制互补
    """
    token = await ensure_token(client)

    order_id = args["order_id"]

    resp = await client.put(
        f"{SPRING_BOOT_URL}/order/cancel/{order_id}",
        headers={
            "Authorization": token,
        },
    )
    resp.raise_for_status()

    body = resp.json()

    if body.get("code") != 200:
        return {
            "success": False,
            "error": body.get("msg", "取消失败"),
        }

    return {
        "success": True,
        "message": f"订单 {order_id} 已取消",
    }


# ==========================================================
# MCP SSE 传输层
# ==========================================================
# MCP 常见传输方式：
#
# 1. stdio
#    常用于 Claude Desktop、Cursor 本地插件
#
# 2. SSE
#    常用于 Dify 这种通过 URL 连接的 Agent 平台
#
# 当前项目使用 SSE：
#
#   Dify
#     ↓ GET /sse
#   建立 SSE 长连接
#     ↓ POST /messages/
#   发送 MCP 初始化消息和工具调用消息
#
# "/messages/" 是 Dify 后续发送 MCP 消息的地址。
# ==========================================================
sse = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    """
    SSE 连接入口。

    Dify 连接 MCP Server 时，会请求：

        GET /sse

    建立连接后，MCP 协议内部会使用 read_stream 和 write_stream
    进行消息收发。

    注意：
    request 必须标注为 Request 类型。
    否则 FastAPI 会把 request 当成普通查询参数，
    导致访问 /sse 返回 422 Unprocessable Entity。
    """
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send,
    ) as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


# ==========================================================
# 应用启动入口
# ==========================================================
def main():
    """
    启动 FastAPI + MCP Server。

    对外暴露两个关键路由：

        GET  /sse
            Dify 连接 MCP Server 的入口

        POST /messages/
            Dify 发送 MCP 初始化消息和 Tool 调用消息的入口

    Dify 配置方式：

        MCP 类型：SSE
        URL：http://host.docker.internal:8000/sse

    如果 Dify 和 MCP 不在同一台机器，
    则需要把 host.docker.internal 换成 MCP 所在机器的 IP。
    """
    fastapi_app = FastAPI(
        title="小兔鲜儿 MCP Server",
        version="1.0.0",
    )

    # 注册 SSE 长连接端点
    fastapi_app.add_api_route(
        "/sse",
        endpoint=handle_sse,
        methods=["GET"],
    )

    # 挂载 MCP 消息处理端点
    fastapi_app.mount(
        "/messages/",
        app=sse.handle_post_message,
    )

    print("=" * 60)
    print("小兔鲜儿 MCP Server 启动成功")
    print(f"监听地址: http://{MCP_HOST}:{MCP_PORT}")
    print(f"SSE 端点: http://localhost:{MCP_PORT}/sse")
    print(f"Spring Boot 后端地址: {SPRING_BOOT_URL}")
    print("=" * 60)

    uvicorn.run(
        fastapi_app,
        host=MCP_HOST,
        port=MCP_PORT,
    )


if __name__ == "__main__":
    main()