from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from . import __version__
from .config import ConfigError, load_config, load_dotenv
from . import evening, google_auth, inbox, morning, telegram
from .heartbeat import run
from .state import StateLocked


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aide", description="Proactive personal assistant (stage 1 + button replies)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    hb = sub.add_parser("heartbeat", help="한 번 점검하고 (옵션) 텔레그램으로 알림")
    hb.add_argument("--send", action="store_true", help="실제 전송 (기본은 드라이런)")
    hb.add_argument("--force", action="store_true", help="조용한 시간대에도 실행")
    hb.add_argument("--config", help="설정 파일 경로 (기본: config.json)")

    mo = sub.add_parser("morning", help="아침 인사 + 어제 작업 요약 (하루 한 번)")
    mo.add_argument("--send", action="store_true", help="실제 전송 (기본은 미리보기)")
    mo.add_argument("--force", action="store_true", help="조용한 시간대에도 실행")
    mo.add_argument("--config", help="설정 파일 경로")

    ev = sub.add_parser("evening", help="저녁 정리 (내일 일정·마감 임박·오늘 작업, 하루 한 번)")
    ev.add_argument("--send", action="store_true", help="실제 전송 (기본은 미리보기)")
    ev.add_argument("--force", action="store_true", help="시각·조용한 시간·중복 확인 무시")
    ev.add_argument("--config", help="설정 파일 경로")

    pl = sub.add_parser("poll", help="텔레그램 버튼 응답을 받아 기록 (읽기 전용)")
    pl.add_argument("--watch", action="store_true", help="계속 대기하며 즉시 처리 (Ctrl+C 로 종료)")
    pl.add_argument("--config", help="설정 파일 경로")

    gl = sub.add_parser("google-login", help="Google 캘린더·메일 읽기 전용 로그인 (브라우저가 열립니다)")
    gl.add_argument("--account", help="계정이 여러 개일 때 로그인할 계정 이름 (config 의 google_accounts name)")
    gl.add_argument("--config", help="설정 파일 경로")

    nt = sub.add_parser("notices", help="공지 페이지 감시 시험: 상태·전송 없이 찾은 항목만 출력")
    nt.add_argument("url", nargs="?", help="한 페이지만 시험 (생략하면 config 의 notice_pages 전체)")
    nt.add_argument("--config", help="설정 파일 경로")

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
        print(f"mail             : {'켜짐' if cfg.mail_enabled else '꺼짐'} / 허용 발신자 {len(cfg.mail_senders)}개 / 시간대 요약 {cfg.mail_recent_hours or '없음'}")
        print(f"evening          : {cfg.evening_hour}시 이후 하루 한 번" if cfg.evening_hour is not None else "evening          : 꺼짐")
        print(f"weather          : {'켜짐' if cfg.weather_enabled else '꺼짐'}")
        accts = cfg.accounts()
        if len(accts) == 1:
            print(f"calendar         : {'켜짐' if cfg.calendar_enabled else '꺼짐'}"
                  f" / client {'있음' if cfg.resolved_google_client().exists() else '없음'}"
                  f" / 로그인 {'됨' if accts[0].token_path.exists() else '안 됨'}")
        else:
            print(f"calendar         : {'켜짐' if cfg.calendar_enabled else '꺼짐'}"
                  f" / client {'있음' if cfg.resolved_google_client().exists() else '없음'} / 계정 {len(accts)}개")
            for a in accts:
                print(f"  계정 {a.name:<10}: 로그인 {'됨' if a.token_path.exists() else '안 됨'}"
                      f" / 메일 링크용 email {'있음' if a.email else '없음'}")
        return 0

    if args.cmd == "notices":
        from datetime import datetime
        from .checks import news_keywords, notice_pages
        pages = [args.url] if args.url else cfg.notice_pages
        if not pages:
            print("notice_pages 가 비어 있습니다 (config.json) 또는 주소를 인자로 주세요.")
            return 2
        for page in pages:
            try:
                n = len(notice_pages.parse_items(notice_pages.decode(news_keywords.fetch_feed(page))))
            except Exception as e:  # noqa: BLE001
                print(f"[{page}] {e}")
                continue
            found = notice_pages.check([page], cfg.notice_keywords, news_keywords.fetch_feed,
                                       datetime.now(), cfg.notice_max_per_page, cfg.notice_exclude)
            print(f"[{page}] 링크 {n}개 중 {len(found)}건 선택")
            for f in found:
                print(f"  • {f.title}" + (f"  ({f.detail})" if f.detail else ""))
        return 0

    if args.cmd == "google-login":
        accts = cfg.accounts()
        names = ", ".join(a.name for a in accts)
        if args.account is not None:
            chosen = [a for a in accts if a.name == args.account]
            if not chosen:
                print(f"[로그인 실패] 알 수 없는 계정 이름입니다: {args.account!r} (설정된 계정: {names or '없음'})", file=sys.stderr)
                return 2
            acct = chosen[0]
        elif len(accts) == 1:
            acct = accts[0]
        else:
            print(f"계정이 여러 개입니다. --account 로 하나를 고르세요: {names}", file=sys.stderr)
            return 2
        try:
            google_auth.login(cfg.resolved_google_client(), acct.token_path)
        except google_auth.GoogleAuthError as e:
            print(f"[로그인 실패] {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "morning":
        return morning.run(cfg, send=args.send, force=args.force)

    if args.cmd == "evening":
        return evening.run(cfg, send=args.send, force=args.force)

    if args.cmd == "poll":
        return _poll(cfg, watch=args.watch)

    return run(cfg, send=args.send, force=args.force)


def _poll(cfg, *, watch: bool) -> int:
    log = logging.getLogger("aide.poll")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        log.error("poll 에는 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다 (.env 확인).")
        return 2
    if not watch:
        if inbox.watcher_active(cfg):
            log.info("실시간 감시(poll --watch)가 켜져 있어 건너뜁니다.")
            return 0
        try:
            n = inbox.poll_once(cfg, token=token, chat_id=chat_id)
        except (telegram.TelegramError, StateLocked) as e:
            log.error("%s", e)
            return 1
        log.info("버튼 응답 %d건 처리", n)
        return 0
    if inbox.watcher_active(cfg):
        log.debug("이미 실시간 감시가 실행 중입니다. 종료합니다.")  # runs every minute: keep the log quiet
        return 0
    wait = 5
    log.info("버튼 응답 대기 중... (Ctrl+C 로 종료)")

    def _on_term(signum, frame):  # systemd stop/restart sends SIGTERM: leave through `finally` so the lock is released
        raise KeyboardInterrupt

    try:
        old_term = signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):   # not the main thread / unsupported: keep the default
        old_term = None
    try:
        while True:
            inbox.touch_watcher(cfg)
            try:
                n = inbox.poll_once(cfg, token=token, chat_id=chat_id, timeout=25)
                wait = 5
                if n:
                    log.info("버튼 응답 %d건 처리", n)
            except (telegram.TelegramError, StateLocked) as e:
                log.warning("%s - %d초 뒤 재시도", e, wait)
                time.sleep(wait)
                wait = min(wait * 2, 60)
    except KeyboardInterrupt:
        return 0
    finally:
        inbox.release_watcher(cfg)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    sys.exit(main())
