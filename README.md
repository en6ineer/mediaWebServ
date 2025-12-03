This is a very simple online media center that works over a local network. To build and run the project, use the following commands in the terminal one after another:
1) python3 -m venv venv
2) source venv/bin/activate
3) pip install fastapi "uvicorn[standard]" aiofiles pillow python-magic
4) python webserv.py
