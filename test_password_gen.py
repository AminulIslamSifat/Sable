import random
import time

def generate_password(length: int = 16) -> str:
chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*"
return "".join(random.choice(chars) for _ in range(length))
def main() -> None:
print("🔐 Secure Password Generator")
for i in range(5):
pwd = generate_password()
print(f" [{i+1}] {pwd}")
time.sleep(0.3)
print("
✅ Done! Stay safe out there.")
if name == "main":
main()
