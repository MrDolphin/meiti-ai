# Creator Workbench V0.1

独立运行的 Windows 本地自媒体创作工作台原型。此项目与 ALQQ 发布仓库业务源码无关；界面与功能由零实现。

## 启动

需要 Python 3.10+，不依赖第三方 Python 包。

1. 下载本仓库 ZIP 并解压。
2. Windows 双击 `start_windows.bat`；或运行 `python app.py`。
3. 浏览器访问 **http://127.0.0.1:8754**。
4. 数据库自动保存在 `data/workbench.sqlite3`。备份时关闭应用后复制 `data` 文件夹。

## 已实现的功能

- 小红书、微信公众号、知乎共 9 个预置创作模板；创建模板、保存不可变版本、查看历史版本。
- 使用 Ollama 或明确启用的 Gemini API 生成文章、在线修改与 AI 改写。
- 保存草稿、重新打开编辑、参考内容手动导入及基础结构拆解。
- 同选题同模型的模板 A/B 对比，保存与导出实验 JSON。
- 多平台账号**记录**与人工发布排期（目前无官方账号授权和自动发布）。
- 默认本地 HTTP 服务，监听 `127.0.0.1`，不向互联网公开数据库或网页。

## AI 配置

默认选择本地 [Ollama](https://ollama.com/)：安装并启动 Ollama，下载模型（例如 `ollama pull qwen2.5:7b`），然后在创作中心输入已安装模型名称。

Gemini 需要自己通过官方渠道获得 API Key，并明确理解账户对应的配额、费用和数据政策。Windows PowerShell 启动前：

```powershell
$env:GEMINI_API_KEY = 'your-api-key'
$env:GEMINI_ALLOW_REQUESTS = '1'
python app.py
```

**注意：** 默认不会发起 Gemini 云端请求；以上开关意味着使用者自行允许请求，无法保证每次 API 调用一定免费。不要提交 API 密钥、Antigravity 登录令牌或本地 `data` 到 GitHub。三个 Antigravity 账号的订阅额度不支持通过本工具复用。

## 功能边界

这是可运行的 **V0.1 原型**，尚未实现自动抓取热榜、平台点赞数指标采集、图片/富文本排版、平台官方授权发布、定时自动提交、Electron EXE 安装包。排期记录仅供人工处理；没有任何代码会向小红书、知乎或公众号提交作品。

## 自动化测试

```powershell
python -m unittest discover -s tests -v
```

## 目录说明

```
app.py                  本地 HTTP API / SQLite / AI 接口
public/index.html       工作台主界面
public/style.css        页面样式
public/app.js           前端交互
tests/test_app.py       不调用云 API 的自动化测试
start_windows.bat       Windows 启动入口
```

