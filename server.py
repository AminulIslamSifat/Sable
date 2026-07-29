from server import app
import uvicorn
from engine.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, reload=False)