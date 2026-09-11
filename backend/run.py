import uvicorn

if __name__ == "__main__":
    # 后台包含长期下载与定时调度线程，热重载会中断正在执行的任务。
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
