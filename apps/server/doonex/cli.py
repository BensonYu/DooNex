import argparse


def main():
    parser = argparse.ArgumentParser(prog="doonex")
    parser.add_argument("command", choices=["serve", "worker", "init"])
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "doonex.api:create_app",
            factory=True,
            host="127.0.0.1",
            port=8741,
            access_log=False,
        )
    elif args.command == "worker":
        from doonex.worker import main as worker_main

        worker_main()
    else:
        from doonex.config import Settings
        from doonex.store import Store

        settings = Settings.load()
        Store(settings.database_url).initialize()
        print("数据库已初始化。访问令牌保存在私有数据目录的 access-token 文件中。")
