"""Scanner Web Server - Website to talk to SANE scanners.

Copyright (C) 2022  CoolCat467

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

__title__ = "Sane Scanner Web Server"
__author__ = "CoolCat467"
__version__ = "2.2.2"
__license__ = "GPLv3"


import contextlib
import functools
import math
import socket
import statistics
import sys
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from configparser import ConfigParser
from dataclasses import dataclass
from enum import IntEnum, auto
from os import makedirs, path
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple, TypeVar, cast
from urllib.parse import urlencode

import sane
import trio
from hypercorn.config import Config
from hypercorn.trio import serve
from PIL import Image
from quart import request
from quart.templating import stream_template
from quart_trio import QuartTrio
from werkzeug.exceptions import HTTPException

from sanescansrv import htmlgen, logger
from sanescansrv.logger import log

if TYPE_CHECKING:
    from werkzeug import Response as WerkzeugResponse

# For some reason error class is not exposed nicely; Let's fix that
SaneError = sane._sane.error
logger.set_title(__title__)

SANE_INITIALIZED = False

Handler = TypeVar("Handler", bound=Callable[..., Awaitable[object]])

if sys.version_info >= (3, 11):
    BaseExceptionGroup_ = BaseExceptionGroup
else:
    BaseExceptionGroup_ = trio.MultiError


def stop_sane() -> None:
    """Exit SANE if started while also updating SANE_INITIALIZED global."""
    global SANE_INITIALIZED
    if SANE_INITIALIZED:
        sane.exit()
    SANE_INITIALIZED = False


def restart_sane() -> None:
    """Start or restart SANE."""
    global SANE_INITIALIZED
    stop_sane()
    sane.init()
    SANE_INITIALIZED = True


async def send_error(
    page_title: str,
    error_body: str,
    return_link: str | None = None,
) -> AsyncIterator[str]:
    """Stream error page."""
    return await stream_template(
        "error_page.html.jinja",
        page_title=page_title,
        error_body=error_body,
        return_link=return_link,
    )


async def get_exception_page(code: int, name: str, desc: str) -> tuple[AsyncIterator[str], int]:
    """Return Response for exception."""
    resp_body = await send_error(
        page_title=f"{code} {name}",
        error_body=desc,
    )
    return (resp_body, code)


def pretty_exception_name(exc: BaseException) -> str:
    """Make exception into pretty text (split by spaces)."""
    exc_str, reason = repr(exc).split("(", 1)
    reason = reason[1:-2]
    words = []
    last = 0
    for idx, char in enumerate(exc_str):
        if char.islower():
            continue
        word = exc_str[last:idx]
        if not word:
            continue
        words.append(word)
        last = idx
    words.append(exc_str[last:])
    error = " ".join(w for w in words if w not in {"Error", "Exception"})
    return f"{error} ({reason})"


def pretty_exception(function: Handler) -> Handler:
    """Make exception pages pretty."""

    @functools.wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        code = None
        name = "Exception"
        desc = None
        try:
            return await function(*args, **kwargs)
        except HTTPException as exception:
            traceback.print_exception(exception)
            code = exception.code
            desc = exception.description
            name = exception.name
        except Exception as exception:
            traceback.print_exception(exception)
            exc_name = pretty_exception_name(exception)
            name = f"Internal Server Error ({exc_name})"
        code = code or 500
        desc = desc or (
            "The server encountered an internal error and "
            + "was unable to complete your request. "
            + "Either the server is overloaded or there is an error "
            + "in the application."
        )
        return await get_exception_page(
            code,
            name,
            desc,
        )

    return cast(Handler, wrapper)


# Stolen from WOOF (Web Offer One File), Copyright (C) 2004-2009 Simon Budig,
# available at http://www.home.unix-ag.org/simon/woof
# with modifications

# Utility function to guess the IP (as a string) where the server can be
# reached from the outside. Quite nasty problem actually.


def find_ip() -> str:
    """Guess the IP where the server can be found from the network."""
    # we get a UDP-socket for the TEST-networks reserved by IANA.
    # It is highly unlikely, that there is special routing used
    # for these networks, hence the socket later should give us
    # the IP address of the default route.
    # We're doing multiple tests, to guard against the computer being
    # part of a test installation.

    candidates: list[str] = []
    for test_ip in ("192.0.2.0", "198.51.100.0", "203.0.113.0"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((test_ip, 80))
        ip_addr: str = sock.getsockname()[0]
        sock.close()
        if ip_addr in candidates:
            return ip_addr
        candidates.append(ip_addr)

    return candidates[0]


def get_devices() -> dict[str, str]:
    """Return dict of SANE name to device."""
    restart_sane()
    # Model name : Device
    devices: dict[str, str] = {}
    for device_name, _vendor, model, _type in sane.get_devices(localOnly=True):
        devices[model] = device_name
    return devices


@dataclass
class DeviceSetting:
    """Setting for device."""

    name: str
    title: str
    options: list[str]
    default: str
    unit: str
    desc: str
    set: str | None = None  # noqa: A003  # Attribute shadows builtin

    def as_argument(self) -> str:
        """Return setting as argument."""
        return f"--{self.name}={self.set if self.set is not None else self.default}"


app: Final = QuartTrio(  # pylint: disable=invalid-name
    __name__,
    static_folder="static",
    template_folder="templates",
)
APP_STORAGE: Final[dict[str, Any]] = {}


def get_device_settings(device_addr: str) -> list[DeviceSetting]:
    """Get device settings."""
    settings: list[DeviceSetting] = []

    try:
        device = sane.open(device_addr)
    except SaneError:
        return []

    for result in device.get_options():
        ##        print(f'\n{result = }')
        if not result[1] or "button" in result[1]:
            continue
        option = sane.Option(result, device)
        if not option.is_settable():
            ##            print("> Not settable")
            continue

        constraints: list[str] = []
        type_ = sane.TYPE_STR[option.type].removeprefix("TYPE_")
        ##        print(f'{type_ = }')
        if type_ not in {"INT", "STRING", "BOOL"}:
            ##            print(f'type {type_!r} is invalid')
            continue
        if type_ == "BOOL":
            constraints = ["1", "0"]
        elif isinstance(option.constraint, tuple):
            if isinstance(option.constraint[0], float):
                continue
            if option.constraint[2] == 0:
                continue
            range_ = range(*option.constraint)
            if len(range_) > 5:
                continue
            constraints = [str(i) for i in range_]
        elif option.constraint is None:
            continue
        else:
            constraints = [str(x) for x in option.constraint]
        if len(constraints) < 2:
            continue

        default = "None"
        with contextlib.suppress(AttributeError, ValueError):
            default = str(getattr(device, option.py_name))
        ##        print(f'{default = }')

        unit = sane.UNIT_STR[option.unit].removeprefix("UNIT_")

        settings.append(
            DeviceSetting(
                name=option.name,
                title=option.title,
                options=constraints,
                default=default,
                unit=unit,
                desc=option.desc,
                ##                set=default,
            ),
        )

    device.close()
    return settings


def display_progress(current: int, total: int) -> None:
    """Display progress of the active scan."""
    print(f"{current / total * 100:.2f}%")


def preform_scan(
    device_name: str,
    out_type: str = "png",
    progress: Callable[[int, int], object] = display_progress,
) -> str:
    """Scan using device and return path."""
    if out_type not in {"pnm", "tiff", "png", "jpeg"}:
        raise ValueError("Output type must be pnm, tiff, png, or jpeg")
    filename = f"scan.{out_type}"
    assert app.static_folder is not None
    filepath = Path(app.static_folder) / filename

    ints = {"TYPE_BOOL", "TYPE_INT"}

    with sane.open(device_name) as device:
        for setting in APP_STORAGE["device_settings"][device_name]:
            name = setting.name.replace("-", "_")
            if setting.set is None:
                continue
            value: str | int = setting.set
            if sane.TYPE_STR[device[name].type] in ints:
                assert isinstance(value, str), f"{value = } {type(value) = }"
                if value.isdigit():
                    value = int(value)
            setattr(device, name, value)
        with device.scan(progress) as image:
            # bounds = image.getbbox()
            image.save(filepath, out_type)

    return filename


class ScanProgress(NamedTuple):
    """Scan Progress Data."""

    current: int
    total: int


class ScanStatus(IntEnum):
    """Scan Status Data."""

    STARTED = auto()
    IN_PROGRESS = auto()
    DONE = auto()


def fake_preform_scan(
    _device_name: str,
    _out_type: str = "png",
    progress: Callable[[int, int], object] = display_progress,
) -> str:
    """Perform fake scan."""
    total = 100
    for current in range(total):
        progress(current, total)
        time.sleep(0.05)
    return "favicon.ico"


SCAN_LOCK = trio.Lock()


async def preform_scan_async(
    device_name: str,
    out_type: str,
    task_status: trio.TaskStatus[Any] = trio.TASK_STATUS_IGNORED,
) -> str:
    """Scan using device and return path."""
    if out_type not in {"pnm", "tiff", "png", "jpeg"}:
        raise ValueError("Output type must be pnm, tiff, png, or jpeg")

    delays = []
    last_time = 0

    def progress(current: int, total: int) -> None:
        """Scan is in progress."""
        nonlocal last_time
        prev_last, last_time = last_time, time.perf_counter_ns()
        delays.append(last_time - prev_last)
        APP_STORAGE["scan_status"] = (
            ScanStatus.IN_PROGRESS,
            ScanProgress(current, total),
            delays,
        )

    async with SCAN_LOCK:
        APP_STORAGE["scan_status"] = (ScanStatus.STARTED,)
        task_status.started()
        last_time = time.perf_counter_ns()
        filename = await trio.to_thread.run_sync(
            preform_scan,  # fake_preform_scan,
            device_name,
            out_type,
            progress,
            thread_name="preform_scan_async",
        )
        APP_STORAGE["scan_status"] = (
            ScanStatus.DONE,
            filename,
        )
    return filename


@app.get("/scan-status")  # type: ignore[type-var]
@pretty_exception
async def scan_status_get() -> AsyncIterator[str] | WerkzeugResponse:
    """Handle scan status GET request."""
    raw_status = APP_STORAGE.get("scan_status")
    if raw_status is None:
        return await send_error(
            "No Scan Currently Running",
            "There are no scan requests running currently. "
            "Start one by pressing the `Scan!` button on the main page.",
        )
    assert raw_status is not None

    status, *data = raw_status

    if status == ScanStatus.DONE:
        filename = data[0]
        return app.redirect(f"/{filename}")

    progress: ScanProgress | None = None
    time_deltas_ns: list[int] | None = None
    delay = 3
    estimated_wait: int = 9999

    if status == ScanStatus.IN_PROGRESS:
        progress, time_deltas_ns = data

        assert isinstance(progress, ScanProgress)
        assert isinstance(time_deltas_ns, list)

        # Estimate when the scan will be done
        # Nanoseconds
        average_wait_ns = statistics.mean(time_deltas_ns)
        delta_total = progress.total - progress.current
        estimated_wait_ns = delta_total * average_wait_ns
        # nanoseconds -> seconds
        estimated_wait = math.ceil(estimated_wait_ns // 1e9)
        delay = max(delay, min(5, estimated_wait))

    return await stream_template(
        "scan-status_get.html.jinja",
        just_started=status == ScanStatus.STARTED,
        progress=progress,
        estimated_wait=estimated_wait,
        refreshes_after=delay,
    )


@app.get("/")  # type: ignore[type-var]
async def root_get() -> AsyncIterator[str]:
    """Handle main page GET request."""
    scanners = {}
    default = "none"

    if APP_STORAGE["scanners"]:
        scanners = {k: k for k in APP_STORAGE["scanners"]}
        # Since radio_select_dict is if comparison for
        # default, if default device does not exist
        # there simply won't be a default shown.
        default = APP_STORAGE["default_device"]

    return await stream_template(
        "root_get.html.jinja",
        scanners=scanners,
        default=default,
    )


@app.post("/")  # type: ignore[type-var]
@pretty_exception
async def root_post() -> WerkzeugResponse | AsyncIterator[str]:
    """Handle page POST."""
    multi_dict = await request.form
    data = multi_dict.to_dict()

    # Validate input
    img_format = data.get("img_format", "png")
    device = APP_STORAGE["scanners"].get(data.get("scanner"), "none")

    if img_format not in {"pnm", "tiff", "png", "jpeg"}:
        return app.redirect("/")
    if device == "none":
        return app.redirect("/scanners")

    raw_status = APP_STORAGE.get("scan_status")

    if raw_status is not None:
        status, *_data = raw_status
        if status != ScanStatus.DONE:
            return await send_error(
                "Scan Already Currently Running",
                "There is a scan request already running. Please wait for the previous scan to complete.",
                return_link="/scan-status",
            )
        APP_STORAGE["scan_status"] = None

    nursery: trio.Nursery | None = APP_STORAGE.get("nursery")
    assert isinstance(nursery, trio.Nursery), "Must be nursery"

    await nursery.start(preform_scan_async, device, img_format)

    return app.redirect("/scan-status")


@app.get("/update_scanners")
@pretty_exception
async def update_scanners_get() -> WerkzeugResponse:
    """Update scanners get handling."""
    APP_STORAGE["scanners"] = get_devices()
    for _model, device in APP_STORAGE["scanners"].items():
        APP_STORAGE["device_settings"][device] = get_device_settings(device)
    return app.redirect("scanners")


@app.get("/scanners")  # type: ignore[type-var]
async def scanners_get() -> AsyncIterator[str]:
    """Scanners page get handling."""
    scanners = {}
    for display in APP_STORAGE.get("scanners", {}):
        scanner_url = urlencode({"scanner": display})
        scanners[f"/settings?{scanner_url}"] = display

    return await stream_template(
        "scanners_get.html.jinja",
        scanners=scanners,
    )


def get_setting_radio(setting: DeviceSetting) -> str:
    """Return setting radio section."""
    options = {x.title(): x for x in setting.options}
    if set(options.keys()) == {"1", "0"}:
        options = {"True": "1", "False": "0"}
    default = setting.default if setting.set is None else setting.set
    return htmlgen.radio_select_box(
        submit_name=setting.name,
        options=options,
        default=default,
        box_title=f"{setting.title} - {setting.desc}",
    )


@app.get("/settings")  # type: ignore[type-var]
async def settings_get() -> AsyncIterator[str] | WerkzeugResponse:
    """Handle settings page GET."""
    scanner = request.args.get("scanner", "none")

    if scanner == "none" or scanner not in APP_STORAGE["scanners"]:
        return app.redirect("/scanners")

    device = APP_STORAGE["scanners"][scanner]
    scanner_settings = APP_STORAGE["device_settings"].get(device, [])

    return await stream_template(
        "settings_get.html.jinja",
        scanner=scanner,
        radios="\n".join(get_setting_radio(setting) for setting in scanner_settings),
    )


@app.post("/settings")
async def settings_post() -> WerkzeugResponse:
    """Handle settings page POST."""
    scanner = request.args.get("scanner", "none")

    if scanner == "none" or scanner not in APP_STORAGE["scanners"]:
        return app.redirect("/scanners")

    device = APP_STORAGE["scanners"][scanner]
    scanner_settings = APP_STORAGE["device_settings"][device]

    valid_settings = {setting.name: idx for idx, setting in enumerate(scanner_settings)}

    multi_dict = await request.form
    data = multi_dict.to_dict()

    for setting_name, new_value in data.items():
        # Input validation
        if setting_name not in valid_settings:
            continue
        idx = valid_settings[setting_name]
        if new_value not in scanner_settings[idx].options:
            continue
        APP_STORAGE["device_settings"][device][idx].set = new_value

    # Return to page for that scanner
    return app.redirect(request.url)


async def serve_async(app: QuartTrio, config_obj: Config) -> None:
    """Serve app within a nursery."""
    async with trio.open_nursery(strict_exception_groups=True) as nursery:
        APP_STORAGE["nursery"] = nursery
        await nursery.start(serve, app, config_obj)


def serve_scanner(
    root_dir: str,
    device_name: str,
    port: int,
    *,
    ip_addr: str | None = None,
) -> None:
    """Asynchronous Entry Point."""
    if not ip_addr:
        ip_addr = find_ip()

    try:
        # Add more information about the address
        location = f"{ip_addr}:{port}"

        config = {
            "bind": [location],
            "worker_class": "trio",
            "errorlog": path.join(
                root_dir,
                "logs",
                time.strftime("log_%Y_%m_%d.log"),
            ),
        }
        app.config["SERVER_NAME"] = location
        app.config["EXPLAIN_TEMPLATE_LOADING"] = False

        app.jinja_options = {
            "trim_blocks": True,
            "lstrip_blocks": True,
        }

        app.add_url_rule("/<path:filename>", "static", app.send_static_file)

        config_obj = Config.from_mapping(config)

        APP_STORAGE["scanners"] = {}
        APP_STORAGE["default_device"] = device_name
        APP_STORAGE["device_settings"] = {}

        print(f"Serving on http://{location}\n(CTRL + C to quit)")

        trio.run(serve_async, app, config_obj)
    except BaseExceptionGroup_ as exc:
        caught = False
        for ex in exc.exceptions:
            if isinstance(ex, KeyboardInterrupt):
                log("Shutting down from keyboard interrupt")
                caught = True
                break
            if isinstance(ex, OSError):
                log(f"Cannot bind to IP address '{ip_addr}' port {port}", 2)
                caught = True
                sys.exit(1)
        if not caught:
            raise


def run() -> None:
    """Run scanner server."""
    root_dir = path.abspath(path.expanduser(path.join("~", ".sanescansrv")))
    if not path.exists(root_dir):
        makedirs(root_dir, exist_ok=True)

    config = ConfigParser()
    conf_file = path.join(root_dir, "config.ini")
    config.read(conf_file)

    target = "None"
    port = 3004
    hostname = "None"

    rewrite = True
    if config.has_section("main"):
        rewrite = False
        if config.has_option("main", "printer"):
            target = config.get("main", "printer")
        else:
            rewrite = True
        if config.has_option("main", "port"):
            raw = config.get("main", "port")
            rewrite = True
            if raw.isdigit():
                port = int(raw)
                rewrite = False
        else:
            rewrite = True
        if config.has_option("main", "hostname"):
            hostname = config.get("main", "hostname")
        else:
            rewrite = True

    if rewrite:
        config.clear()
        config.read_dict(
            {
                "main": {
                    "printer": target,
                    "port": port,
                    "hostname": hostname,
                },
            },
        )
        with open(conf_file, "w", encoding="utf-8") as config_file:
            config.write(config_file)

    print(f"Default Printer: {target}\nPort: {port}\nHostname: {hostname}")
    print(f"PIL Image Version: {Image.__version__}\n")

    if target == "None":
        print("No default device in config file.")

    ip_address = None
    if hostname != "None":
        ip_address = hostname

    serve_scanner(root_dir, target, port, ip_addr=ip_address)


def sane_run() -> None:
    """Run but also handle initializing and un-initializing SANE."""
    try:
        run()
    finally:
        stop_sane()


if __name__ == "__main__":
    sane_run()
