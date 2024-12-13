"""
Copyright (C) 2023-2024 Fern Lane, Homeland-sees automated surveillance camera project

Licensed under the GNU Affero General Public License, Version 3.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.gnu.org/licenses/agpl-3.0.en.html

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from bot_handler import BotHandler
from gpio_handler import GPIOHandler
from recorder import Recorder

# Homeland-sees version
__version__ = "2.0.dev1"

# Default config file
CONFIG_FILE = "config.json"

# Logging location
LOGS_DIR = "logs"

# This file will store timestamp of last interrupt and number of interrupts per current day
INTERRUPTS_COUNTER_FILE = ".interrupts"

# Logging level
LOGGING_LEVEL = logging.INFO

# Delay in the main loop
CYCLE_DELAY = 0.1

# How long to wait between recording to prevent ffmpeg errors
SECONDS_BETWEEN_RECORDINGS = 10

# Minimum size of video to send (in bytes)
MINIMUM_FILE_SIZE = 1000000


def logging_setup() -> None:
    """Sets up logging format and level"""
    # Create logs directory is not exists
    if not os.path.exists(LOGS_DIR):
        os.makedirs(LOGS_DIR)

    # Create logs formatter
    log_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)-8s] [%(filename)10s:%(lineno)4s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Setup logging into file
    file_handler = logging.FileHandler(
        os.path.join(LOGS_DIR, datetime.now().strftime("%Y_%m_%d__%H_%M_%S") + ".log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(log_formatter)

    # Setup logging into console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)

    # Add all handlers and setup level
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    root_logger.setLevel(LOGGING_LEVEL)

    # Log test message
    logging.info("Logging setup is complete")


def parse_args() -> argparse.Namespace:
    """Parses cli arguments

    Returns:
        argparse.Namespace: parsed arguments
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        default=os.getenv("CONFIG_FILE", CONFIG_FILE),
        type=str,
        required=False,
        help="config.json file location (you can also use CONFIG_FILE env variable)",
        metavar="CONFIG_FILE",
    )
    parser.add_argument("-v", "--version", action="version", version=__version__)
    return parser.parse_args()


def main() -> None:
    """Main entry"""
    # Parse arguments
    args = parse_args()

    # Save start timestamp
    datetime_started = datetime.today()

    # Initialize logging
    logging_setup()

    # Log software version
    logging.info(f"Homeland-sees version: {__version__}")

    # Load config
    with open(args.config, "r", encoding="utf-8") as config_io:
        config: dict[str, Any] = json.load(config_io)

    # Initialize classes
    bot_handler_ = BotHandler(config)
    gpio_handler_ = GPIOHandler(config)
    recorder_ = Recorder(config)

    # Start bot polling
    bot_handler_.start()

    # Start main loop (blocking)
    logging.info("Starting main loop")
    recording = False
    recording_stopped_time = 0
    door_closed_timer = 0
    pause_requested_prev = False
    files_sent: list[str] = []
    output_dir: Optional[str] = None
    while True:
        try:
            # Update start timestamp after resuming
            if bot_handler_.pause_requested != pause_requested_prev:
                pause_requested_prev = bot_handler_.pause_requested
                if not bot_handler_.pause_requested:
                    datetime_started = datetime.today()

            # Read current state
            door_opened = gpio_handler_.door_get()
            test_flag = bot_handler_.test_flag
            if test_flag:
                bot_handler_.test_flag = False

            # It's time to start recording!
            if (test_flag or (door_opened and not bot_handler_.pause_requested)) and not recording:
                # Retrieve datetime
                door_opened_datetime = datetime.today()

                # Send message
                logging.warning(f"Received {'test' if test_flag else 'door'} interrupt!")
                # Test
                if test_flag:
                    bot_handler_.queue.put("text_" + door_opened_datetime.strftime(config["event_test_text"]))

                # Actual interrupt
                else:
                    # Read last interrupt timestamp
                    interrupts_num = 0
                    last_interrupt_timestamp = 0
                    try:
                        if os.path.exists(INTERRUPTS_COUNTER_FILE):
                            with open(INTERRUPTS_COUNTER_FILE, "r", encoding="utf-8") as file_io:
                                timestamp_s, interrupts_s = file_io.read().strip().split(" ")
                                last_interrupt_timestamp = int(timestamp_s)
                                interrupts_num = int(interrupts_s)
                                logging.info(f"Last was @ {last_interrupt_timestamp}. Total: {interrupts_num}")
                    except Exception as e:
                        logging.error(f"Unable to read {INTERRUPTS_COUNTER_FILE} file", exc_info=e)

                    # Reset total number of interrupts if it's new day
                    today_start = datetime.combine(datetime.today(), datetime.min.time())
                    today_end = today_start + timedelta(days=1) - timedelta(seconds=1)
                    if (
                        datetime.fromtimestamp(last_interrupt_timestamp) < today_start
                        or datetime.fromtimestamp(last_interrupt_timestamp) > today_end
                    ):
                        logging.info("Last was more then 1 day ago. Resetting total number of interrupts")
                        interrupts_num = 0

                    # Write new interrupt
                    interrupts_num += 1
                    last_interrupt_timestamp = int(time.time())
                    try:
                        with open(INTERRUPTS_COUNTER_FILE, "w+", encoding="utf-8") as file_io:
                            file_io.write(f"{last_interrupt_timestamp} {interrupts_num}\n")
                    except Exception as e:
                        logging.error(f"Unable to write {INTERRUPTS_COUNTER_FILE} file", exc_info=e)

                    # Build message with all info
                    message_text = door_opened_datetime.strftime(config["event_text"])
                    message_text += "\n\n" + datetime_started.strftime(config["event_text_start_time"])
                    message_text += "\n" + config["event_text_interrupts_num"].format(interrupts_num=interrupts_num)
                    bot_handler_.queue.put("text_" + message_text)

                # Turn light on
                logging.info("Turning light on")
                gpio_handler_.light_set(True)

                # Reset timer
                door_closed_timer = 0

                # Start recording
                output_dir = None
                files_sent.clear()
                if not recorder_.recording:
                    # Wait a bit
                    if time.time() - recording_stopped_time < SECONDS_BETWEEN_RECORDINGS:
                        seconds_to_sleep = SECONDS_BETWEEN_RECORDINGS - (time.time() - recording_stopped_time)
                        logging.info(f"Waiting {seconds_to_sleep:.2f}s to prevent errors in future")
                        time.sleep(seconds_to_sleep)

                    output_dir = recorder_.start(door_opened_datetime)

                    # Set recording flag
                    recording = True

                # Report error
                if output_dir is None:
                    bot_handler_.queue.put("text_" + config["error_starting_recording"])

            # Stop recording if door was closed,or pause requested
            if recording and (not door_opened or bot_handler_.pause_requested):
                # Start timer in case of pause requested or door was closed
                if door_closed_timer == 0:
                    logging.info(
                        f"Door closed or pause requested! Stopping recording after {config['record_extra']:.2f}s"
                    )
                    door_closed_timer = time.time()

                # Time passed
                elif time.time() - door_closed_timer > float(config["record_extra"]):
                    # Clear timer
                    door_closed_timer = 0

                    # Clear flag
                    recording = False

                    # Turn off the light
                    logging.info("Turning light off")
                    gpio_handler_.light_set(False)

                    # Stop recording and send file or report error
                    if recorder_.recording:
                        recorder_.stop()
                        if output_dir:
                            recording_stopped_time = time.time()
                    else:
                        bot_handler_.queue.put("text_" + config["error_stopping_recording"])

            # Cancel timer if door opened again
            if door_opened and recording and door_closed_timer != 0:
                logging.warning("Received door interrupt again!")
                door_closed_timer = 0

            # Send files
            if output_dir:
                files = [f for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f))]
                files = [f for f in files if f not in files_sent]
                files.sort()
                if len(files) != 0 and (len(files) > 1 or not recording):
                    file = files.pop(0)
                    files_sent.append(file)

                    file_abs = os.path.join(output_dir, file)
                    filesize = os.path.getsize(file_abs)

                    if filesize > MINIMUM_FILE_SIZE:
                        logging.info(f"Found new file to send: {file_abs}")
                        bot_handler_.queue.put("video_" + file_abs)
                    else:
                        logging.warning(f"Found new file to send: {file_abs} but it's size is only: {filesize}B")

                    if len(files) == 0 and not recording:
                        logging.info("No more files to send")
                        output_dir = None

            # Wait come time before next cycle
            time.sleep(CYCLE_DELAY)

        # Exit
        except (KeyboardInterrupt, SystemExit):
            logging.warning("Exit requested!")
            break

    # If we're here, exit requested
    recorder_.stop()
    gpio_handler_.cleanup()
    bot_handler_.stop()
    logging.warning("Homeland-sees exited")


if __name__ == "__main__":
    main()
