"""声明配置键、类型和默认值，统一加载录制、推送与凭据设置。"""

import sys
from typing import Annotated, get_args, get_origin, get_type_hints

from src.config.io import _safe_float, _safe_int, read_config_bool, read_config_value
from src.config.store import ConfigStore

language: Annotated[str, "录制设置", "language"] = ""
recording_limit: Annotated[int, "录制设置", "最大同时录制数(0为不限制)"] = 0
max_request: Annotated[int, "录制设置", "同一时间访问网络的线程数"] = 3
delay_default: Annotated[int, "录制设置", "循环时间(秒)"] = 120
local_delay_default: Annotated[int, "录制设置", "排队读取网址时间(秒)"] = 0
loop_time: Annotated[bool, "录制设置", "是否显示循环秒数"] = False
show_url: Annotated[bool, "录制设置", "是否显示直播源地址"] = False
video_record_quality: Annotated[str, "录制设置", "原画|超清|高清|标清|流畅"] = "原画"
video_save_type: Annotated[str, "录制设置", "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频"] = "ts"
split_video_by_time: Annotated[bool, "录制设置", "分段录制是否开启"] = False
split_time: Annotated[str, "录制设置", "视频分段时间(秒)"] = "1800"
create_time_file: Annotated[bool, "录制设置", "生成时间字幕文件"] = False
auto_update_anchor_name: Annotated[bool, "录制设置", "是否自动更新主播名(是/否)"] = True
hls_collection_enabled: Annotated[bool, "录制设置", "是否启用HLS采集(是/否)"] = True
hls_collection_exclude_platforms: Annotated[list[str], "录制设置", "HLS采集排除平台(逗号分隔)"] = []
enable_danmaku: Annotated[bool, "录制设置", "是否录制弹幕(是/否)"] = False
enable_danmaku_monitor: Annotated[bool, "录制设置", "是否弹幕监控(是/否)"] = False
danmaku_split_time: Annotated[float, "录制设置", "弹幕分片时长(秒)"] = 1800.0
danmaku_platforms: Annotated[list[str], "录制设置", "弹幕录制平台(逗号分隔)"] = [
    "斗鱼直播",
    "B站直播",
    "虎牙直播",
    "抖音直播",
    "TwitchTV",
]
use_proxy: Annotated[bool, "录制设置", "是否使用代理ip(是/否)"] = True
proxy_addr: str | None = None
proxy_addr_bak: Annotated[str, "录制设置", "代理地址"] = ""
enable_proxy_platform: Annotated[str, "录制设置", "使用代理录制的平台(逗号分隔)"] = (
    "tiktok, soop, pandalive, winktv, flextv, popkontv, twitch, liveme, showroom, chzzk, shopee, shp, youtu, faceit"
)
enable_proxy_platform_list: list[str] | None = None
extra_enable_proxy: Annotated[str, "录制设置", "额外使用代理录制的平台(逗号分隔)"] = ""
extra_enable_proxy_platform_list: list[str] | None = None
enable_https_recording: bool = False
disk_space_limit: Annotated[float, "录制设置", "录制空间剩余阈值(gb)"] = 1.0
douyin_min_interval: float = 3.0  # 两次抖音请求的最小间隔（秒）
douyin_dm_cooldown: Annotated[int, "录制设置", "抖音私信自动录制冷却时间(秒)"] = 60
douyin_dm_daily_limit: Annotated[int, "录制设置", "抖音私信自动录制每日上限(个)"] = 20
douyin_dm_enabled: Annotated[bool, "录制设置", "抖音私信分享自动录制(是/否)"] = False
douyin_dm_interval: Annotated[int, "录制设置", "抖音私信轮询间隔(秒)"] = 60
douyin_dm_lookback_minutes: Annotated[int, "录制设置", "抖音私信自动录制回溯分钟"] = 30
douyin_dm_quality: Annotated[str, "录制设置", "抖音私信自动录制画质"] = "原画"
douyin_dm_require_live: Annotated[bool, "录制设置", "抖音私信自动录制仅在播(是/否)"] = False
douyin_follow_interval: Annotated[int, "录制设置", "抖音关注轮询间隔(秒)"] = 60
douyin_follow_mode: Annotated[str, "录制设置", "抖音直播检测方式(逐个/关注优先)"] = "逐个"
video_save_path: Annotated[str, "录制设置", "直播保存路径(不填则默认)"] = ""
record_directory_template: Annotated[str, "录制设置", "录制目录模板"] = ""
max_record_seconds: Annotated[int, "录制设置", "单次录制时长上限(秒,0为不限制)"] = 6 * 60 * 60
clean_emoji: Annotated[bool, "录制设置", "是否去除名称中的表情符号"] = True
filename_by_title: Annotated[bool, "录制设置", "保存文件名是否包含标题"] = False
folder_by_author: Annotated[bool, "录制设置", "保存文件夹是否以作者区分"] = True
folder_by_time: Annotated[bool, "录制设置", "保存文件夹是否以时间区分"] = False
folder_by_title: Annotated[bool, "录制设置", "保存文件夹是否以标题区分"] = False
converts_to_h264: Annotated[bool, "录制设置", "mp4格式重新编码为h264"] = False
converts_to_mp4: Annotated[bool, "录制设置", "录制完成后自动转为mp4格式"] = False
delete_origin_file: Annotated[bool, "录制设置", "追加格式后删除原文件"] = False
is_run_script: Annotated[bool, "录制设置", "是否录制完成后执行自定义脚本"] = False
custom_script: Annotated[str | None, "录制设置", "自定义脚本执行命令"] = ""
video_save_type_list: tuple[str, ...] = ("FLV", "MKV", "TS", "MP4", "MP3音频", "M4A音频", "MP3", "M4A")

live_status_push: Annotated[str, "推送配置", "直播状态推送渠道"] = ""
push_message_title: Annotated[str, "推送配置", "自定义推送标题"] = "直播间状态更新通知"
begin_show_push: Annotated[bool, "推送配置", "开播推送开启(是/否)"] = True
begin_push_message_text: Annotated[str, "推送配置", "自定义开播推送内容"] = ""
over_show_push: Annotated[bool, "推送配置", "关播推送开启(是/否)"] = False
over_push_message_text: Annotated[str, "推送配置", "自定义关播推送内容"] = ""
disable_record: Annotated[bool, "推送配置", "只推送通知不录制(是/否)"] = False
push_check_seconds: Annotated[int, "推送配置", "直播推送检测频率(秒)"] = 1800
dingtalk_api_url: Annotated[str, "推送配置", "钉钉推送接口链接"] = ""
dingtalk_phone_num: Annotated[str, "推送配置", "钉钉通知@对象(填手机号)"] = ""
dingtalk_is_atall: Annotated[bool, "推送配置", "钉钉通知@全体(是/否)"] = False
xizhi_api_url: Annotated[str, "推送配置", "微信推送接口链接"] = ""
bark_msg_api: Annotated[str, "推送配置", "bark推送接口链接"] = ""
bark_msg_level: Annotated[str, "推送配置", "bark推送中断级别"] = "active"
bark_level_overrides: dict[str, str] = {}
bark_msg_ring: Annotated[str, "推送配置", "bark推送铃声"] = "bell"
email_host: Annotated[str, "推送配置", "SMTP邮件服务器"] = ""
email_password: Annotated[str, "推送配置", "发件人密码(授权码)"] = ""
login_email: Annotated[str, "推送配置", "邮箱登录账号"] = ""
sender_email: Annotated[str, "推送配置", "发件人邮箱"] = ""
sender_name: Annotated[str, "推送配置", "发件人显示昵称"] = ""
to_email: Annotated[str, "推送配置", "收件人邮箱"] = ""
smtp_port: Annotated[str, "推送配置", "SMTP邮件服务器端口"] = ""
open_smtp_ssl: Annotated[bool, "推送配置", "是否使用SMTP服务SSL加密(是/否)"] = True
tg_chat_id: Annotated[str, "推送配置", "tg聊天id(个人或者群组id)"] = ""
tg_token: Annotated[str, "推送配置", "tgapi令牌"] = ""
ntfy_api: Annotated[str, "推送配置", "ntfy推送地址"] = ""
ntfy_tags: Annotated[str, "推送配置", "ntfy推送标签"] = "tada"
ntfy_email: Annotated[str, "推送配置", "ntfy推送邮箱"] = ""
pushplus_token: Annotated[str, "推送配置", "pushplus推送token"] = ""

sooplive_username: Annotated[str, "账号密码", "sooplive账号"] = ""
sooplive_password: Annotated[str, "账号密码", "sooplive密码"] = ""
flextv_username: Annotated[str, "账号密码", "flextv账号"] = ""
flextv_password: Annotated[str, "账号密码", "flextv密码"] = ""
popkontv_username: Annotated[str, "账号密码", "popkontv账号"] = ""
popkontv_partner_code: Annotated[str, "账号密码", "partner_code"] = "P-00001"
popkontv_password: Annotated[str, "账号密码", "popkontv密码"] = ""
popkontv_access_token: Annotated[str, "Authorization", "popkontv_token"] = ""
twitcasting_account_type: Annotated[str, "账号密码", "twitcasting账号类型"] = "normal"
twitcasting_username: Annotated[str, "账号密码", "twitcasting账号"] = ""
twitcasting_password: Annotated[str, "账号密码", "twitcasting密码"] = ""

dy_cookie: Annotated[str, "Cookie", "抖音cookie"] = ""
ks_cookie: Annotated[str, "Cookie", "快手cookie"] = ""
tiktok_cookie: Annotated[str, "Cookie", "tiktok_cookie"] = ""
hy_cookie: Annotated[str, "Cookie", "虎牙cookie"] = ""
douyu_cookie: Annotated[str, "Cookie", "斗鱼cookie"] = ""
yy_cookie: Annotated[str, "Cookie", "yy_cookie"] = ""
bili_cookie: Annotated[str, "Cookie", "B站cookie"] = ""
xhs_cookie: Annotated[str, "Cookie", "小红书cookie"] = ""
bigo_cookie: Annotated[str, "Cookie", "bigo_cookie"] = ""
blued_cookie: Annotated[str, "Cookie", "blued_cookie"] = ""
sooplive_cookie: Annotated[str, "Cookie", "sooplive_cookie"] = ""
netease_cookie: Annotated[str, "Cookie", "netease_cookie"] = ""
qiandurebo_cookie: Annotated[str, "Cookie", "千度热播_cookie"] = ""
pandatv_cookie: Annotated[str, "Cookie", "pandatv_cookie"] = ""
maoerfm_cookie: Annotated[str, "Cookie", "猫耳fm_cookie"] = ""
winktv_cookie: Annotated[str, "Cookie", "winktv_cookie"] = ""
flextv_cookie: Annotated[str, "Cookie", "flextv_cookie"] = ""
look_cookie: Annotated[str, "Cookie", "look_cookie"] = ""
liveme_cookie: Annotated[str, "Cookie", "liveme_cookie"] = ""
huajiao_cookie: Annotated[str, "Cookie", "huajiao_cookie"] = ""
liuxing_cookie: Annotated[str, "Cookie", "liuxing_cookie"] = ""
showroom_cookie: Annotated[str, "Cookie", "showroom_cookie"] = ""
acfun_cookie: Annotated[str, "Cookie", "acfun_cookie"] = ""
changliao_cookie: Annotated[str, "Cookie", "changliao_cookie"] = ""
yinbo_cookie: Annotated[str, "Cookie", "yinbo_cookie"] = ""
yingke_cookie: Annotated[str, "Cookie", "yingke_cookie"] = ""
zhihu_cookie: Annotated[str, "Cookie", "zhihu_cookie"] = ""
chzzk_cookie: Annotated[str, "Cookie", "chzzk_cookie"] = ""
haixiu_cookie: Annotated[str, "Cookie", "haixiu_cookie"] = ""
vvxqiu_cookie: Annotated[str, "Cookie", "vvxqiu_cookie"] = ""
yiqilive_cookie: Annotated[str, "Cookie", "17live_cookie"] = ""
langlive_cookie: Annotated[str, "Cookie", "langlive_cookie"] = ""
pplive_cookie: Annotated[str, "Cookie", "pplive_cookie"] = ""
six_room_cookie: Annotated[str, "Cookie", "6room_cookie"] = ""
lehaitv_cookie: Annotated[str, "Cookie", "lehaitv_cookie"] = ""
huamao_cookie: Annotated[str, "Cookie", "huamao_cookie"] = ""
shopee_cookie: Annotated[str, "Cookie", "shopee_cookie"] = ""
youtube_cookie: Annotated[str, "Cookie", "youtube_cookie"] = ""
taobao_cookie: Annotated[str, "Cookie", "taobao_cookie"] = ""
jd_cookie: Annotated[str, "Cookie", "jd_cookie"] = ""
faceit_cookie: Annotated[str, "Cookie", "faceit_cookie"] = ""
migu_cookie: Annotated[str, "Cookie", "migu_cookie"] = ""
lianjie_cookie: Annotated[str, "Cookie", "lianjie_cookie"] = ""
laixiu_cookie: Annotated[str, "Cookie", "laixiu_cookie"] = ""
picarto_cookie: Annotated[str, "Cookie", "picarto_cookie"] = ""
baidu_cookie: Annotated[str, "Cookie", "baidu_cookie"] = ""
weibo_cookie: Annotated[str, "Cookie", "weibo_cookie"] = ""
kugou_cookie: Annotated[str, "Cookie", "kugou_cookie"] = ""
twitch_cookie: Annotated[str, "Cookie", "twitch_cookie"] = ""
twitcasting_cookie: Annotated[str, "Cookie", "twitcasting_cookie"] = ""


# 保存声明时的默认值，热加载不能把上一轮的值当成缺省值。
_FIELDS = {
    name: (
        *get_args(annotation),
        ",".join(globals()[name]) if get_origin(get_args(annotation)[0]) is list else globals()[name],
    )
    for name, annotation in get_type_hints(sys.modules[__name__], include_extras=True).items()
    if get_origin(annotation) is Annotated
}


def read_https_recording(config: ConfigStore) -> bool:
    """读取 HTTPS 开关，按现有优先级迁移旧键。"""
    if config.has_option("录制设置", "是否启用https录制"):
        return read_config_bool(config, "录制设置", "是否启用https录制", False)
    if config.has_option("录制设置", "是否强制启用https录制"):
        legacy = read_config_bool(config, "录制设置", "是否强制启用https录制", False)
        return read_config_bool(config, "录制设置", "是否启用https录制", legacy)
    return read_config_bool(config, "录制设置", "是否启用https录制", False)


def load(config: ConfigStore) -> None:
    """按字段声明读取配置，归一化后发布本轮生效设置。"""
    values = {}
    for name, (value_type, section, key, default) in _FIELDS.items():
        if name == "custom_script" and not values["is_run_script"]:
            values[name] = None
            continue
        if value_type is bool:
            value = read_config_bool(config, section, key, default)
        else:
            raw = read_config_value(config, section, key, default)
            if value_type is int:
                value = _safe_int(raw, default)
            elif value_type is float:
                value = _safe_float(raw, default)
            elif get_origin(value_type) is list:
                value = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
            else:
                value = raw
        values[name] = value

    values["delay_default"] = max(30, values["delay_default"])
    values["local_delay_default"] = max(0, values["local_delay_default"])
    values["push_check_seconds"] = max(30, values["push_check_seconds"])
    split_seconds = _safe_int(values["split_time"], 1800)
    values["split_time"] = str(split_seconds if split_seconds > 0 else 1800)
    values["max_record_seconds"] = max(0, values["max_record_seconds"])
    save_type = values["video_save_type"].upper()
    values["video_save_type"] = save_type if save_type in video_save_type_list else "TS"
    values["proxy_addr"] = values["proxy_addr_bak"] if values["use_proxy"] else None
    for source, target in (
        ("enable_proxy_platform", "enable_proxy_platform_list"),
        ("extra_enable_proxy", "extra_enable_proxy_platform_list"),
    ):
        raw = values[source]
        values[target] = raw.replace("，", ",").split(",") if raw else None
    values["enable_https_recording"] = read_https_recording(config)
    values["bark_level_overrides"] = (
        dict(config.items("Bark主播中断级别")) if config.has_section("Bark主播中断级别") else {}
    )
    globals().update(values)
