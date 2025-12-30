from flask import Flask, render_template

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/paper")
def paper():
    return "<h1>White paper coming soon</h1>"


@app.route("/sim")
def sim():
    return "<h1>Simulation coming soon</h1>"


if __name__ == "__main__":
    app.run(debug=True)
