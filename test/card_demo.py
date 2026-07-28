
"""Demo file for testing tool activity cards."""


def greet(name: str, excited: bool = False) -> str:
    suffix = "!!!" if excited else "!"
    return f"Hello, {name}{suffix}"


def add(a: int, b: int) -> int:
    return a + b


if __name__ == "__main__":
    print(greet("Sifat", excited=True))
    print(f"2 + 3 = {add(2, 3)}")
