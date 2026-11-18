import random
import math


def clamp(value, low, high):
    return max(low, min(high, value))


def get_day_fraction(sim_time):
    return (sim_time.hour * 60 + sim_time.minute) / (24 * 60)


def generate_environment(sim_time):
    """
    Generate fake environment data for simulation.
    """
    day_frac = get_day_fraction(sim_time)

    sunlight = max(0, math.sin(math.pi * (day_frac - 0.25) / 0.5)) * 900

    temperature = 22 + 6 * math.sin(2 * math.pi * (day_frac - 0.2))
    temperature += random.uniform(-0.8, 0.8)

    humidity = 62 - 10 * math.sin(2 * math.pi * (day_frac - 0.2))
    humidity += random.uniform(-2, 2)
    humidity = clamp(humidity, 35, 90)

    return sunlight, temperature, humidity