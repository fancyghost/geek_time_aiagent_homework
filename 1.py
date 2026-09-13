import requests,time

# 1. 您的 Cookie（从浏览器复制，确保格式正确）
cookies = {
    'PHPSESSID': 'c6ogbej1tvtdjr1s8sv8hnsn24', 
    'server_name_session':'2edb941ed9fb6d3a84aeaccbed71bf15'
      # 替换为实际的 session ID
    # 可能还有其他 cookie，建议全部复制
}

# 2. 请求头（至少包含 User-Agent 和 Content-Type）
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...',
    'Content-Type': 'application/x-www-form-urlencoded',
    # 可选：Referer 等
}

# 3. 表单数据（键值对）
# 必须包含 act=choose 以及所有 choosed[] 的值（已选课程的ID）
# 还要包括所有 hidden 字段（如果有的话）
# data = {
#     'act': 'choose',
#     'choosed[]': ['3778', '3787', '3794', '3805', '3814', '3793', '3806', '3817'],  # 您要提交的课程ID列表
#     # 如果还有其他 hidden 字段，也要加入，例如：
#     # 'id': '66',
#     # 'other_field': 'value'
# }

data1 = {
    'act': 'choose',
    'choosed[]': ['3778', '3787', '3794', '3805', '3814',  '3793'],  # 您要提交的课程ID列表
}
data2 = {
    'act': 'choose',
    'choosed[]': ['3778', '3787', '3794', '3805', '3814',  '3806'],  # 您要提交的课程ID列表
}
data3 = {
    'act': 'choose',
    'choosed[]': ['3778', '3787', '3794', '3805', '3814',  '3817'],  # 您要提交的课程ID列表
}

data_list = [data1,data2,data3]

# 4. 发送 POST 请求（URL 为当前活动详情页）
url = 'https://slhsx.jxgypt.com/?mod=acDetail&id=66'  # 替换为实际 URL

while True:
    for data in data_list:
        response = requests.post(url, data=data, cookies=cookies, headers=headers)
        try:
            result = response.json()
            if result.get('status') == 'success':
                print('提交成功！')
            else:
                print('提交失败：', result.get('msg', '未知错误'))
        except:
            print('响应内容：', response.text)
        time.sleep(1)
