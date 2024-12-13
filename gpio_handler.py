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

import logging

import OPi.GPIO as GPIO


class GPIOHandler:
    def __init__(self, config: dict):
        self._config = config

        # self._time_started = time.time()  # For debug

        # Initialize GPIOs
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
        GPIO.output(
            self._config["pin_light"],
            (not state) if self._config["pin_light_inverted"] else state,
        )

    def door_get(self) -> bool:
        """Reads current door state

        Returns:
            bool: True if door is opened, False if door is closed
        """
        # return time.time() - self._time_started < 45  # For debug
        door_state = True if GPIO.input(self._config["pin_door_interrupt"]) else False
        return (not door_state) if self._config["pin_door_interrupt_inverted"] else door_state

    def cleanup(self) -> None:
        """Turns of light and does cleanup"""
        self.light_set(False)
        GPIO.cleanup()
