from napcat.command import BoolField, StrField, CommandSchema, QQField, DurationField


class GreetingSchema(CommandSchema):
  content = StrField(description="欢迎语内容；不带则保留现有欢迎语", default="")
  enabled = BoolField(description="是否开启入群问候", default=True)
  # 三态：不带（None）= 保持现值，0 = 关闭撤回，正数 = 启用并设为该时长。
  # 默认写成 None 而不是 0：管理命令不该把配置文件里手写的延迟悄悄重置掉。
  recall_delay = DurationField(default=None, min=0, max=86400,
                               description="欢迎语发出后多久撤回（秒，0=不撤回；可用 s/m/h，如 2m）")

class BanSchema(CommandSchema):
    user_id = QQField(required=True, description="目标QQ")
    duration = DurationField(default=600, min=0, max=2592000, description="禁言时长")
    reason = StrField(default="", max_length=200, description="原因")

class KickSchema(CommandSchema):
    user_id = QQField(required=True, description="目标QQ")
    reject = BoolField(default=False, description="是否拒绝再次加群")

class GroupInfoSchema(CommandSchema):
    user_id = QQField(required=False, description="查看的QQ（默认自己）")
