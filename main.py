import os

import basic
import value

# 先初始化，在依次需要传给其他类
Value = value.Main()
# 将初始化
logs = basic.Go().main()
Value.logger = logs

Value.logger.info("你好啊，我是你的第一条日志")




