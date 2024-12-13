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

import gc
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pwd import getpwnam
from queue import Empty, Queue
from typing import Any, Optional

# In seconds
START_RECORDING_TIMEOUT = 3.5
STOP_RECORDING_TIMEOUT = 7.0

# Number of attempts to start recording
START_ATTEMPTS = 3


class Recorder:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

        self.recording = False

        self._attempts = 0
        self._process: subprocess.Popen | None = None
        self._process_out_capture_enabled = False
        self._process_out_queue = Queue(-1)

    def start(self, event_time: datetime) -> str | None:
        """Tries to start recording

        Args:
            event_time (datetime): time of door interrupt

        Returns:
            str | None: path to output directory if recording started
        """
        try:
            # Generate output dir path
            subdir = event_time.strftime(self._config["output_subdir_format"])
            output_dir = os.path.join(self._config["output_directory"], subdir)
            if not os.path.exists(output_dir):
                output_dir_abs = os.path.abspath(output_dir)
                logging.info(f"Creating directory: {output_dir_abs}")
                os.makedirs(output_dir_abs, exist_ok=True)

                # Change ownership
                try:
                    if self._config.get("ffmpeg_command_user"):
                        user = getpwnam(self._config["ffmpeg_command_user"])
                        os.chown(os.path.abspath(self._config["output_directory"]), user.pw_uid, user.pw_gid)
                        os.chown(output_dir_abs, user.pw_uid, user.pw_gid)
                except Exception as e:
                    logging.warning(f"Unable to change directory ownership: {e}")

            logging.info(f"Recording into directory: {output_dir}")

            # Prepare the ffmpeg command
            ffmpeg_command: list[str] = self._config["ffmpeg_command"].copy()
            for i, arg in enumerate(ffmpeg_command):
                if "{output_dir}" in arg:
                    ffmpeg_command[i] = arg.replace("{output_dir}", output_dir)

            # Start ffmpeg process
            self._attempts += 1
            logging.info(
                f"[Attempt {self._attempts}] Starting recording process using command: {' '.join(ffmpeg_command)}"
            )
            self._process = subprocess.Popen(
                ffmpeg_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                user=self._config.get("ffmpeg_command_user"),
                extra_groups=self._config.get("ffmpeg_command_groups"),
            )

            # Start capturing threads
            self._process_out_capture_enabled = True
            while True:
                try:
                    self._process_out_queue.get(block=False)
                except Empty:
                    break
            threading.Thread(target=self._ffmpeg_stdout_reader, daemon=True).start()
            threading.Thread(target=self._ffmpeg_stderr_reader, daemon=True).start()

            # Ensure the process starts
            if self._process.poll() is None:
                watchdog_timer = time.time()
                logging.info("Waiting for ffmpeg to start recording...")

                while time.time() - watchdog_timer < START_RECORDING_TIMEOUT:
                    try:
                        line: Optional[str] = self._process_out_queue.get(block=False)
                    except Empty:
                        line = None

                    if not line or "frame=" not in line:
                        time.sleep(0.01)
                        continue

                    self.recording = True
                    self._process_out_capture_enabled = False
                    self._attempts = 0
                    logging.info(f"Recording started. PID: {self._process.pid}")
                    return output_dir

                # Stop in case of error
                logging.error("Timeout waiting for ffmpeg to start!")
                self.stop(from_self=True)
                if self._attempts >= START_ATTEMPTS:
                    self._attempts = 0
                    return None

                # Try again?
                logging.warning("Trying again")
                time.sleep(1)
                return self.start(event_time)
            else:
                logging.error("ffmpeg failed to start")
                return None

        except Exception as e:
            logging.error("Error starting recording!", exc_info=e)
            return None

    def stop(self, from_self: bool = False) -> None:
        """Stops the recording process

        Args:
            from_self (bool, optional): if set to True, self._attempts will not be reset. Defaults to False
        """
        if self._process is None:
            return

        if not from_self:
            self._attempts = 0

        try:
            logging.info("Stopping recording...")

            self._process_out_capture_enabled = False
            while True:
                try:
                    self._process_out_queue.get(block=False)
                except Empty:
                    break

            if self._process.poll() is None:
                try:
                    # Normal exit
                    self._process.terminate()
                    self._process.wait(timeout=STOP_RECORDING_TIMEOUT)
                    logging.info("ffmpeg process finished successfully")

                except subprocess.TimeoutExpired:
                    # Let ffmpeg to terminate itself
                    logging.warning("ffmpeg process did not terminate in time, sending termination 4 times...")
                    try:
                        for _ in range(4):
                            if self._process.poll() is not None:
                                break
                            self._process.terminate()
                            time.sleep(0.1)
                    except:
                        pass

                    # Wait a bit to let ffmpeg exit by itself
                    time.sleep(1)

                    # Check
                    if self._process.poll() is None:
                        logging.warning("ffmpeg process did not terminate in time, sending kill...")
                        self._process.kill()

                except Exception as e:
                    logging.error("Error stopping recording process!", exc_info=e)

            # Very hard kill
            logging.info(f"Calling hard kill (for cleanup): {' '.join(self._config['ffmpeg_kill_command'])}")
            for _ in range(3):
                ffmpeg_killer = subprocess.Popen(
                    self._config["ffmpeg_kill_command"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                ffmpeg_killer.wait(timeout=STOP_RECORDING_TIMEOUT)
                time.sleep(0.1)

            # Wait
            while self._process.poll() is None:
                time.sleep(0.1)

            # Final cleanup
            self._process = None
            self.recording = False
            logging.info("Recording stopped")
            gc.collect()
            self._process = None

        except Exception as e:
            logging.error("Error stopping recording!", exc_info=e)

    def _ffmpeg_stdout_reader(self) -> None:
        """Reads ffmpeg's stdout and puts each line in self._process_out_messages"""
        while self._process.poll() is None:
            line_b: bytes = self._process.stdout.readline()
            try:
                line = line_b.decode("utf-8", errors="replace").strip()
                if self._process_out_capture_enabled:
                    self._process_out_queue.put(line)
                logging.info(f"[ffmpeg stdout] {line}")
            except Exception as e:
                logging.warning(f"Unable to decode line from ffmpeg stdout: {e}")

    def _ffmpeg_stderr_reader(self) -> None:
        """Reads ffmpeg's stderr and puts each line in self._process_out_messages"""
        while self._process.poll() is None:
            line_b: bytes = self._process.stderr.readline()
            try:
                line = line_b.decode("utf-8", errors="replace").strip()
                if self._process_out_capture_enabled:
                    self._process_out_queue.put(line)
                logging.info(f"[ffmpeg stderr] {line}")
            except Exception as e:
                logging.warning(f"Unable to decode line from ffmpeg stderr: {e}")
