from flask import Flask, render_template

app = Flask(__name__)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/paper")
def paper():
    return render_template("paper.html")


@app.route("/sim")
def sim():
    return render_template("sim.html")


if __name__ == "__main__":
    app.run(debug=True)
