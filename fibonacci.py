# Fibonacci sequence printer – prints numbers starting from 1 forever


def fibonacci():
    a, b = 0, 1
    while True:
        yield b
        a, b = b, a + b


if __name__ == "__main__":
    for num in fibonacci():
        print(num)
