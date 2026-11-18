class FakeActuators:
    """
    Simulation version of the ESP32 peristaltic pumps, plus placeholders
    for upcoming grow light (led_on) and fan (fan_on) hardware.
    """

    def __init__(self):
        self.states = {
            "water_pump_on": False,
            "drain_pump_on": False,
            "nutrient_pump_on": False,
            "ph_up_pump_on": False,
            "ph_down_pump_on": False,
            # None = not received / not available on the live node yet.
            "led_on": None,
            "fan_on": None,
            "pump_on": False,
        }

    def set_water_pump(self, state: bool):
        self.states["water_pump_on"] = state
        self.states["pump_on"] = state

    def set_pump(self, state: bool):
        self.set_water_pump(state)

    def set_drain_pump(self, state: bool):
        self.states["drain_pump_on"] = state

    def set_nutrient_pump(self, state: bool):
        self.states["nutrient_pump_on"] = state

    def set_ph_up_pump(self, state: bool):
        self.states["ph_up_pump_on"] = state

    def set_ph_down_pump(self, state: bool):
        self.states["ph_down_pump_on"] = state

    def set_led(self, state):
        self.states["led_on"] = state

    def set_fan(self, state):
        self.states["fan_on"] = state

    def get_states(self):
        return self.states.copy()
