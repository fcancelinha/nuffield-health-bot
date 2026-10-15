import asyncio
import signal

import structlog

from nuffield_bot.composition import build_booking_service


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )


async def main() -> None:
    configure_logging()
    log = structlog.get_logger()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with build_booking_service() as service:
        runner = asyncio.create_task(service.run_forever())
        await stop.wait()
        log.info("bot.stopping")
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
    log.info("bot.stopped")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()