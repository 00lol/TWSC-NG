# -*- coding:utf-8 -*-
# Pycharm
# 邮箱发送模块
# 2024/11/17 14:59
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

class AutoEmail:
    def __init__(self, logger, sender="", password="", start=False):
        self.logger = logger
        self.sender = sender        # 发送者邮箱
        self.password = password    # 发送者邮箱的授权码
        # 常见邮箱服务商，端口统一用 SSL 465
        self.email_type_list = [
            {"smtp": "smtp.163.com", "port": 465, "name": "163"},
            {"smtp": "smtp.126.com", "port": 465, "name": "126"},
            {"smtp": "smtp.qq.com", "port": 465, "name": "qq"},
            {"smtp": "smtp.gmail.com", "port": 465, "name": "gmail"},
            {"smtp": "smtp.office365.com", "port": 587, "name": "outlook"},
        ]
        # 判断是否非法邮箱
        if not self.sender:
            raise ValueError("邮箱不能为空")
        if not self.password:
            raise ValueError("邮箱授权码不能为空")
        # 自动判断服务商
        for i in self.email_type_list:
            if self.sender.endswith(f"@{i['name']}.com") or f"@{i['name']}." in self.sender:
                self.logger.info(f"邮箱类型为{i['name']}")
                self.email_type = i["name"]
                self.url = i["smtp"]
                self.port = i["port"]
                break
        else:
            raise ValueError("不支持的邮箱类型")
        self.logger.info("邮箱初始化完成,全局生效！")

    def send(self, receiver: str, html_content: str, html_title: str):
        """发送 HTML 邮件，返回 (bool, 说明)"""
        try:
            if not self.email_type:
                return False, "邮箱未启用"
            message = MIMEText(html_content, "html", "utf-8")
            message["Subject"] = html_title
            message["From"] = formataddr(("TWSC-NG", self.sender))
            message["To"] = receiver

            with smtplib.SMTP_SSL(self.url, self.port) as smtp:
                smtp.login(self.sender, self.password)
                smtp.sendmail(self.sender, receiver, message.as_string())
                smtp.quit()
            return True, "发送成功"
        except smtplib.SMTPRecipientsRefused:
            self.logger.warning(f"邮箱发送失败，收件人不存在: {receiver}")
            return False, "未能找到收件人"
        except smtplib.SMTPAuthenticationError as e:
            self.logger.error(f"邮箱认证失败，请检查账号与授权码: {e}")
            return False, "邮箱认证失败"
        except Exception as e:
            self.logger.error(f"邮箱发送失败: {e}")
            return False, "其他未知错误"

    def send_code(self, receiver: str, code: str, scene: str = "register", expire_minutes: int = 5):
        """发送验证码邮件，返回 (bool, 说明)"""
        titles = {
            "register": "注册验证码",
            "reset": "重置密码验证码",
            "login": "登录验证码",
            "bind": "邮箱绑定验证码",
        }
        title = titles.get(scene, "验证码")
        html = f"""
        <div style="font-family: -apple-system, 'Microsoft YaHei', sans-serif; max-width: 520px; margin: 0 auto; padding: 24px;">
            <h2 style="color: #222;">TWSC-NG {title}</h2>
            <p style="color: #555; font-size: 14px;">您正在进行{title.replace('验证码', '')}操作，验证码如下（{expire_minutes} 分钟内有效）：</p>
            <div style="font-size: 34px; font-weight: bold; letter-spacing: 8px; color: #1677ff;
                        background: #f5f8ff; border-radius: 8px; padding: 16px 0; text-align: center;">
                {code}
            </div>
            <p style="color: #888; font-size: 12px; margin-top: 20px;">
                如果这不是您本人的操作，请忽略本邮件。请勿将验证码告知他人。
            </p>
            <p style="color: #aaa; font-size: 12px;">—— TWSC-NG 系统邮件，请勿直接回复</p>
        </div>
        """
        return self.send(receiver, html, title)

    # 返回实例
    def main(self):
        return self.send
