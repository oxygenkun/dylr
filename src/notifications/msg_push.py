import base64
import http.client
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import cast

from loguru import logger

from src.support import i18n

no_proxy_handler: urllib.request.ProxyHandler = urllib.request.ProxyHandler({})
_PUSH_HTTP_HANDLERS: tuple[type[urllib.request.BaseHandler], ...] = (
    urllib.request.UnknownHandler,
    urllib.request.HTTPHandler,
    urllib.request.HTTPDefaultErrorHandler,
    urllib.request.HTTPRedirectHandler,
    urllib.request.HTTPErrorProcessor,
)


def _build_push_opener() -> urllib.request.OpenerDirector:
    built = urllib.request.OpenerDirector()
    built.add_handler(no_proxy_handler)
    for klass in _PUSH_HTTP_HANDLERS:
        built.add_handler(klass())
    built.add_handler(urllib.request.HTTPSHandler())
    return built


opener: urllib.request.OpenerDirector = _build_push_opener()
headers: dict[str, str] = {"Content-Type": "application/json"}


_PUSH_PLACEHOLDER_ALIASES: dict[str, str] = {
    "{ROOM_NAME}": "ROOM_NAME",
    "{TIME}": "TIME",
    "[直播间名称]": "ROOM_NAME",
    "[时间]": "TIME",
}


def render_push_template(template: str, room_name: str, push_time: str) -> str:
    """把**用户可编辑的模板**（config.toml 自定义标题/开播/关播推送内容）里的占位符换成真实值。"""
    values = {"ROOM_NAME": room_name, "TIME": push_time}
    rendered = template
    for placeholder, key in _PUSH_PLACEHOLDER_ALIASES.items():
        rendered = rendered.replace(placeholder, values[key])
    return rendered


def render_push_content(template: str, room_name: str, push_time: str) -> str:
    """渲染**内置推送文案**：先 tr 取译文，再把译文里的占位符换成真实值。"""
    if _PUSH_PLACEHOLDER_ALIASES["[直播间名称]"] in template:
        return render_push_template(template, room_name, push_time)
    return render_push_template(i18n.tr(template), room_name, push_time)


def _checked_push_url(url: str) -> str:
    """推送地址准入校验（M-07）：只放行 http/https，并拒绝「内部用途名字」。"""
    from urllib.parse import urlsplit

    from src.network.targets import host_rejection_reason

    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(i18n.tr("推送地址协议不受支持（仅允许 http/https）: {scheme}", scheme=parts.scheme or "-"))
    host = parts.hostname or ""
    if not host:
        raise ValueError(i18n.tr("推送地址缺少主机名"))
    reason = host_rejection_reason(host, allow_local_targets=True)
    if reason is not None:
        raise ValueError(i18n.tr("推送地址指向内部目标，已拒绝: {reason}", reason=reason))
    return url


def _mask_secret(secret: str) -> str:
    """脱敏密钥：保留前后各 2 位，其余以 * 遮挡，防日志泄露。"""
    if not secret:
        return ""
    if len(secret) <= 6:
        return "****"
    return f"{secret[:2]}{'*' * (len(secret) - 4)}{secret[-2:]}"


def _mask_url(url: str, *, mask_last_segment: bool = False) -> str:
    """脱敏推送地址：丢弃 query 与疑似密钥路径段，仅供日志展示、不影响实际请求。"""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        segs = [s for s in parts.path.split("/") if s]
        masked_segs: list[str] = []
        for idx, seg in enumerate(segs):
            if seg.startswith("bot") and len(seg) > 4:
                masked_segs.append("bot****")  # Telegram token
            elif (
                seg.endswith(".send")
                or seg.lower() in ("key", "sendmessage")
                or idx == len(segs) - 1
                and mask_last_segment
            ):
                masked_segs.append("****")
            elif len(seg) > 12 and "sendmessage" not in seg.lower():
                masked_segs.append("****")  # 疑似长密钥（Server酱/Bark 末段）
            else:
                masked_segs.append(seg)
        masked_path = "/" + "/".join(masked_segs) if masked_segs else ""
        return urlunsplit((parts.scheme, parts.netloc, masked_path, "", ""))
    except Exception:
        return _mask_secret(url)


def dingtalk(url: str, content: str, number: str | None = None, is_atall: bool = False) -> dict[str, list[str | int]]:
    """钉钉群机器人推送文本消息，支持 @手机号/全体，返回成功与失败地址列表。"""
    success: list[str | int] = []
    error: list[str | int] = []
    api_list = url.replace("，", ",").split(",") if url.strip() else []
    for api in api_list:
        at_payload: dict[str, object] = {"isAtAll": is_atall}
        if number:
            at_payload["atMobiles"] = [number]
        json_data = {
            "msgtype": "text",
            "text": {"content": content},
            "at": at_payload,
        }
        try:
            _ = _checked_push_url(api)
            data = json.dumps(json_data).encode("utf-8")
            req = urllib.request.Request(api, data=data, headers=headers)
            with cast(http.client.HTTPResponse, opener.open(req, timeout=10)) as response:
                json_str = response.read().decode("utf-8")
            resp_data: dict[str, object] = cast(dict[str, object], json.loads(json_str))
            if resp_data.get("errcode") == 0:
                success.append(api)
            else:
                error.append(api)
                logger.warning(
                    i18n.tr(
                        "钉钉推送失败, 推送地址：{masked_api}, {errmsg}",
                        masked_api=_mask_url(api),
                        errmsg=resp_data.get("errmsg", i18n.tr("未知错误")),
                    )
                )
        except Exception as e:
            error.append(api)
            logger.warning(
                i18n.tr(
                    "钉钉推送失败, 推送地址：{masked_api}, 错误信息: {type_name}: {e}",
                    masked_api=_mask_url(api),
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    return {"success": success, "error": error}


def xizhi(url: str, title: str, content: str) -> dict[str, list[str | int]]:
    """通过 Server酱/微信 推送消息（url 为推送地址，title/content 为内容）。"""
    success: list[str | int] = []
    error: list[str | int] = []
    api_list = url.replace("，", ",").split(",") if url.strip() else []
    for api in api_list:
        json_data = {"title": title, "content": content}
        try:
            _ = _checked_push_url(api)
            data = json.dumps(json_data).encode("utf-8")
            req = urllib.request.Request(api, data=data, headers=headers)
            with cast(http.client.HTTPResponse, opener.open(req, timeout=10)) as response:
                json_str = response.read().decode("utf-8")
            resp_data: dict[str, object] = cast(dict[str, object], json.loads(json_str))
            if resp_data.get("code") == 200:
                success.append(api)
            else:
                error.append(api)
                logger.warning(
                    i18n.tr(
                        "微信推送失败, 推送地址：{masked_api}, 失败信息：{msg}",
                        masked_api=_mask_url(api, mask_last_segment=True),
                        msg=resp_data.get("msg", i18n.tr("未知错误")),
                    )
                )
        except Exception as e:
            error.append(api)
            logger.warning(
                i18n.tr(
                    "微信推送失败, 推送地址：{masked_api}, 错误信息: {type_name}: {e}",
                    masked_api=_mask_url(api, mask_last_segment=True),
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    return {"success": success, "error": error}


def _reject_smtp_newline(kind: str, value: str) -> None:
    """SMTP 头注入防护（与 src/web_config._reject_newline 同款语义）。"""
    if value and ("\n" in value or "\r" in value):
        raise ValueError(i18n.tr("{kind} 含换行符，禁止用于邮件头", kind=kind))


def send_email(
    email_host: str,
    login_email: str,
    email_pass: str,
    sender_email: str,
    sender_name: str,
    to_email: str,
    title: str,
    content: str,
    smtp_port: str | None = None,
    open_ssl: bool = True,
) -> dict[str, list[str]]:
    """通过 SMTP 发送邮件（支持 SSL/非SSL），返回成功与失败收件人列表。"""
    receivers = to_email.replace("，", ",").split(",") if to_email.strip() else []
    smtp_obj: smtplib.SMTP | smtplib.SMTP_SSL | None = None

    ctx = ssl.create_default_context()

    try:
        _reject_smtp_newline(i18n.tr("邮件标题"), title)
        _reject_smtp_newline(i18n.tr("发件人地址"), sender_email)
        _reject_smtp_newline(i18n.tr("发件人名称"), sender_name)
        for rcpt in receivers:
            _reject_smtp_newline(i18n.tr("收件人地址"), rcpt)

        message = MIMEMultipart()
        send_name = base64.b64encode(sender_name.encode("utf-8")).decode()
        message["From"] = f"=?UTF-8?B?{send_name}?= <{sender_email}>"
        message["Subject"] = str(Header(title, "utf-8"))
        if len(receivers) == 1:
            message["To"] = receivers[0]

        t_apart = MIMEText(content, "plain", "utf-8")
        message.attach(t_apart)

        if open_ssl:
            try:
                port = int(smtp_port) if smtp_port else 465
            except ValueError:
                port = 465
            smtp_obj = smtplib.SMTP_SSL(email_host, port, timeout=10, context=ctx)
        else:
            try:
                port = int(smtp_port) if smtp_port else 25
            except ValueError:
                port = 25
            smtp_obj = smtplib.SMTP(email_host, port, timeout=10)
            try:
                _ = smtp_obj.ehlo()
                _ = smtp_obj.starttls(context=ctx)
                _ = smtp_obj.ehlo()
            except smtplib.SMTPException as e:
                logger.warning(
                    i18n.tr(
                        "SMTP 服务器不支持 STARTTLS，本次将以明文发送邮箱授权码（存在被窃听风险，"
                        "建议在配置中改为启用 SSL 加密）: {type_name}",
                        type_name=type(e).__name__,
                    )
                )
        assert smtp_obj is not None
        _ = smtp_obj.login(login_email, email_pass)
        _ = smtp_obj.sendmail(sender_email, receivers, message.as_string())
        return {"success": receivers, "error": []}
    except ValueError as e:
        logger.warning(i18n.tr("邮件推送被拒绝（疑似头注入）: {e}", e=e))
        return {"success": [], "error": receivers}
    except smtplib.SMTPException as e:
        logger.warning(
            i18n.tr(
                "邮件推送失败, 推送邮箱：{to_email}, 错误信息: {type_name}: {e}",
                to_email=to_email,
                type_name=type(e).__name__,
                e=e,
            )
        )
        return {"success": [], "error": receivers}
    except Exception as e:
        logger.warning(
            i18n.tr(
                "邮件推送失败, 推送邮箱：{to_email}, 错误信息: {type_name}: {e}",
                to_email=to_email,
                type_name=type(e).__name__,
                e=e,
            )
        )
        return {"success": [], "error": receivers}
    finally:
        if smtp_obj:
            try:
                _ = smtp_obj.quit()
            except smtplib.SMTPException, OSError:
                pass


def tg_bot(chat_id: str | int, token: str, content: str) -> dict[str, list[str | int]]:
    """Telegram Bot 推送文本消息，返回成功与失败聊天ID列表。"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        json_data = {"chat_id": chat_id, "text": content}
        data = json.dumps(json_data).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
        with cast(http.client.HTTPResponse, opener.open(req, timeout=15)) as response:
            json_str = response.read().decode("utf-8")
        resp_data: dict[str, object] = cast(dict[str, object], json.loads(json_str))
        if resp_data.get("ok") is True:
            return {"success": [str(chat_id)], "error": []}
        error_detail = resp_data.get("description", i18n.tr("未知错误"))
        logger.warning(
            i18n.tr(
                "tg推送失败, 聊天ID：{chat_id}, 推送地址：{masked_url}, 失败信息:{error_detail}",
                chat_id=chat_id,
                masked_url=_mask_url(url),
                error_detail=error_detail,
            )
        )
        return {"success": [], "error": [str(chat_id)]}
    except Exception as e:
        logger.warning(
            i18n.tr(
                "tg推送失败, 聊天ID：{chat_id}, 推送地址：{masked_url}, 错误信息: {type_name}: {e}",
                chat_id=chat_id,
                masked_url=_mask_url(url),
                type_name=type(e).__name__,
                e=e,
            )
        )
        return {"success": [], "error": [str(chat_id)]}


def bark(
    api: str,
    title: str = "message",
    content: str = "test",
    level: str = "active",
    badge: int = 1,
    auto_copy: int = 1,
    sound: str = "",
    icon: str = "",
    group: str = "",
    is_archive: int = 1,
    url: str = "",
) -> dict[str, list[str | int]]:
    """Bark（iOS）推送通知，返回成功与失败地址列表。"""
    success: list[str | int] = []
    error: list[str | int] = []
    api_list = api.replace("，", ",").split(",") if api.strip() else []
    for _api in api_list:
        json_data = {
            "title": title,
            "body": content,
            "level": level,
            "badge": badge,
            "autoCopy": auto_copy,
            "sound": sound,
            "icon": icon,
            "group": group,
            "isArchive": is_archive,
            "url": url,
        }
        try:
            _ = _checked_push_url(_api)
            data = json.dumps(json_data).encode("utf-8")
            req = urllib.request.Request(_api, data=data, headers=headers)
            with cast(http.client.HTTPResponse, opener.open(req, timeout=10)) as response:
                json_str = response.read().decode("utf-8")
            resp_data: dict[str, object] = cast(dict[str, object], json.loads(json_str))
            if resp_data.get("code") == 200:
                success.append(_api)
            else:
                error.append(_api)
                logger.warning(
                    i18n.tr(
                        "Bark推送失败, 推送地址：{masked_api}, 失败信息：{message}",
                        masked_api=_mask_url(_api, mask_last_segment=True),
                        message=resp_data.get("message", i18n.tr("未知错误")),
                    )
                )
        except Exception as e:
            error.append(_api)
            logger.warning(
                i18n.tr(
                    "Bark推送失败, 推送地址：{masked_api}, 错误信息: {type_name}: {e}",
                    masked_api=_mask_url(_api, mask_last_segment=True),
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    return {"success": success, "error": error}


def ntfy(
    api: str,
    title: str = "message",
    content: str = "test",
    tags: str | list[str] = "tada",
    priority: int = 3,
    action_url: str = "",
    attach: str = "",
    filename: str = "",
    click: str = "",
    icon: str = "",
    delay: str = "",
    email: str = "",
    call: str = "",
) -> dict[str, list[str | int]]:
    """ntfy 跨平台推送通知（支持 tags/优先级/附件等），返回成功与失败列表。"""
    success: list[str | int] = []
    error: list[str | int] = []
    api_list = api.replace("，", ",").split(",") if api.strip() else []
    if isinstance(tags, str):
        tags = tags.replace("，", ",").split(",") if tags else ["partying_face"]
    elif not tags:
        tags = ["partying_face"]
    actions = [{"action": "view", "label": "view live", "url": action_url}] if action_url else []
    for _api in api_list:
        try:
            server, topic = _api.rsplit("/", maxsplit=1)
            json_data = {
                "topic": topic,
                "title": title,
                "message": content,
                "tags": tags,
                "priority": priority,
                "attach": attach,
                "filename": filename,
                "click": click,
                "actions": actions,
                "markdown": False,
                "icon": icon,
                "delay": delay,
                "email": email,
                "call": call,
            }

            _ = _checked_push_url(server)
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(server, data=data, headers=headers)
            with cast(http.client.HTTPResponse, opener.open(req, timeout=10)) as response:
                json_str = response.read().decode("utf-8")
            resp_data: dict[str, object] = cast(dict[str, object], json.loads(json_str))
            if "error" not in resp_data:
                success.append(_api)
            else:
                error.append(_api)
                logger.warning(
                    i18n.tr(
                        "ntfy推送失败, 推送地址：{masked_api}, 失败信息：{error}",
                        masked_api=_mask_url(_api, mask_last_segment=True),
                        error=resp_data["error"],
                    )
                )
        except urllib.error.HTTPError as e:
            error.append(_api)
            try:
                error_msg = e.read().decode("utf-8")
                error_detail = cast(dict[str, object], json.loads(error_msg)).get("error", str(e))
            except Exception:
                error_detail = str(e)
            finally:
                e.close()
            logger.warning(
                i18n.tr(
                    "ntfy推送失败, 推送地址：{masked_api}, 错误信息:{error_detail}",
                    masked_api=_mask_url(_api, mask_last_segment=True),
                    error_detail=error_detail,
                )
            )
        except Exception as e:
            error.append(_api)
            logger.warning(
                i18n.tr(
                    "ntfy推送失败, 推送地址：{masked_api}, 错误信息: {type_name}: {e}",
                    masked_api=_mask_url(_api, mask_last_segment=True),
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    return {"success": success, "error": error}


def pushplus(token: str, title: str, content: str) -> dict[str, list[str | int]]:
    """PushPlus 推送（token+title+content），返回成功与失败 token 列表。"""
    success: list[str | int] = []
    error: list[str | int] = []
    token_list = token.replace("，", ",").split(",") if token.strip() else []

    for _token in token_list:
        json_data = {"token": _token, "title": title, "content": content}

        try:
            url = "https://www.pushplus.plus/send"
            data = json.dumps(json_data).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers)
            with cast(http.client.HTTPResponse, opener.open(req, timeout=10)) as response:
                json_str = response.read().decode("utf-8")
            resp_data: dict[str, object] = cast(dict[str, object], json.loads(json_str))

            if resp_data.get("code") == 200:
                success.append(_token)
            else:
                error.append(_token)
                logger.warning(
                    i18n.tr(
                        "PushPlus推送失败, Token：{masked_token}, 失败信息：{msg}",
                        masked_token=_mask_secret(_token),
                        msg=resp_data.get("msg", i18n.tr("未知错误")),
                    )
                )
        except Exception as e:
            error.append(_token)
            logger.warning(
                i18n.tr(
                    "PushPlus推送失败, Token：{masked_token}, 错误信息: {type_name}: {e}",
                    masked_token=_mask_secret(_token),
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    return {"success": success, "error": error}


if __name__ == "__main__":
    send_title = "直播通知"
    send_content = "张三 开播了！"

    webhook_api = ""
    phone_number = ""
    is_atall = ""

    xizhi_api = "https://xizhi.qqoq.net/xxxxxxxxx.send"

    tg_token = ""
    tg_chat_id = 000000

    bark_url = "https://xxx.xxx.com/key/"

    _ = ntfy  # 保留引用，避免 lint 将 ntfy 判为未使用导入/定义

    pushplus_token = ""
