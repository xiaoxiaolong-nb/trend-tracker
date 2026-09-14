# 云端化改造记录 — 趋势抄底追踪器

## 时间
2026-09-14

## 目标
让项目在 Render.com 免费云平台运行，移除本地 .day 文件依赖。

## 改动文件

### 1. app.py
- **L72**: `VIP = r'C:/new_tdx/vipdoc'` → `VIP = r'__vipdoc_unavailable__'`
  - 去除本地 .day 路径引用
  - app.py 中本身无 .day 直接读取，腾讯接口优先，fallback 也无需本地文件
- **`__main__` 块末尾**：新增云端/本地分支逻辑
  ```python
  # === 云端适配 ===
  import os as _os
  RENDER = _os.environ.get('RENDER', '')
  PORT = _os.environ.get('PORT', '')
  is_cloud = bool(RENDER or PORT)
  if is_cloud:
      _port = int(PORT) if PORT else 10000
      print(f'[cloud] 启动云端模式 port={_port}')
      sio.run(app, host='0.0.0.0', port=_port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
  else:
      # 本地
      sio.run(app, host='127.0.0.1', port=5001, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
  ```

### 2. requirements.txt（新建）
- 固定版本 pip freeze 风格，含 gevent + gevent-websocket（gunicorn 所需）

### 3. Procfile（新建）
```
web: gunicorn app:app --worker-class gevent --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
```

## 验证
- ✅ `python -m py_compile app.py` → SYNTAX_OK
- ✅ app.py 中无 `.day` / `load_day` / `open(.*\.day` 引用

## Render 部署注意事项
1. 将 app.py、requirements.txt、Procfile、templates/、static/ 推送到 GitHub
2. Render 关联 GitHub repo，Build Command 自动 `pip install -r requirements.txt`
3. Start Command 自动读取 Procfile
4. 免费版 512MB RAM / 0.6 CPU CPU，注意 akshare 数据拉取延迟
