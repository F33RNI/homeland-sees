"""
Copyright (C) 2023-2025 Fern Lane, Homeland-sees automated surveillance camera project

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

import logging
import threading

# pyright: reportMissingImports=false, reportPossiblyUnboundVariable=false
_gpio_debug = False
try:
    try:
        import OPi.GPIO as GPIO
    except (ImportError, ModuleNotFoundError):
        import RPi.GPIO as GPIO
except (ImportError, ModuleNotFoundError, RuntimeError):
    print("WARNING! No OPi.GPIO or RPi.GPIO package! Running in GPIO debug mode")
    _gpio_debug = True


class GPIOHandler:
    def __init__(self, config: dict):
        self._config = config

        # Debug mode
        if _gpio_debug:
            self._door_state_debug = False
            self._door_emulation_running = False

            def door_emulation_thread() -> None:
                while self._door_emulation_running:
                    try:
                        state_str = "Opened" if self._door_state_debug else "Closed"
                        logging.warning(
                            "[GPIO Debug] Type 1 and press Enter to emulate door opened "
                            f"or type 0 and press Enter to emulate door closed. (Current state: {state_str})"
                        )
                        door_state = input().strip()
                        if not door_state:
                            continue
                        if door_state[0] == "1":
                            logging.warning("[GPIO Debug] Setting door state to opened")
                            self._door_state_debug = True
                        elif door_state[0] == "0":
                            logging.warning("[GPIO Debug] Setting door state to closed")
                            self._door_state_debug = False
                        else:
                            logging.warning(f"[GPIO Debug] Unknown door state: {door_state}")
                    except (KeyboardInterrupt, SystemExit):
                        break

            logging.info("[GPIO Debug] Starting door emulation thread")
            self._door_emulation_running = True
            threading.Thread(target=door_emulation_thread).start()

        # Initialize GPIOs
        else:
            logging.info("Initializing GPIOs")
            GPIO.setmode(GPIO.BOARD)
            GPIO.setup(self._config["pin_door_interrupt"], GPIO.IN)
            GPIO.setup(self._config["pin_light"], GPIO.OUT)
            self.light_set(False)

    def light_set(self, state: bool) -> None:
        """Turns light ON or OFF

        Args:
            state (bool): True to enable light, False to disable
        """
        # Debug mode
        if _gpio_debug:
            logging.info(f"[GPIO Debug] Light: {state}")
        else:
            GPIO.output(self._config["pin_light"], (not state) if self._config["pin_light_inverted"] else state)

    def door_get(self) -> bool:
        """Reads current door state

        Returns:
            bool: True if door is opened, False if door is closed
        """
        if _gpio_debug:
            return self._door_state_debug
        else:
            door_state = True if GPIO.input(self._config["pin_door_interrupt"]) else False
            return (not door_state) if self._config["pin_door_interrupt_inverted"] else door_state

    def cleanup(self) -> None:
        """Turns of light and does cleanup"""
        self.light_set(False)
        if _gpio_debug:
            logging.info("[GPIO Debug] cleanup() called")
        else:
            GPIO.cleanup()

    def stop(self) -> None:
        """Stops GPIO debug thread"""
        if _gpio_debug:
            self._door_emulation_running = False
            logging.warning("[GPIO Debug] Press Enter")
