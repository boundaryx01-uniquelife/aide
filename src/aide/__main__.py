from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .config import ConfigError, load_config, load_dotenv
from .heartbeat import run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aide", description="Proactive personal assistant (stage 1)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    hb = sub.add_parser("heartbeat", help="한 번 점검하고 (옵션) 텔레그램으로 알림")
    hb.add_argument("--send", action="store_true", help="실제 전송 (기본은 드라이런)")
    hb.add_argument("--force", action="store_true", help="조용한 시간대에도 실행")
    hb.add_argument("--config", help="설정 파일 경로 (기본: config.json)")

    sc = sub.add_parser("selfcheck", help="설정과 환경 점검 (비밀값은 출력하지 않음)")
    sc.add_argument("--config", help="설정 파일 경로")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    if args.cmd == "selfcheck":
        print(f"watch_repos      : {len(cfg.watch_repos)}개")
        print(f"news_feeds       : {len(cfg.news_feeds)}개 / keywords {len(cfg.news_keywords)}개")
        print(f"quiet hours      : {cfg.quiet_start} - {cfg.quiet_end}")
        print(f"state file       : {cfg.resolved_state_path()}")
        print(f"TELEGRAM_BOT_TOKEN: {'설정됨' if os.environ.get('TELEGRAM_BOT_TOKEN') else '없음'}")
        print(f"TELEGRAM_CHAT_ID  : {'설정됨' if os.environ.get('TELEGRAM_CHAT_ID') else '없음'}")
        return 0

    return run(cfg, send=args.send, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
