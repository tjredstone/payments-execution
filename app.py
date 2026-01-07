from flask import Flask, render_template
from sim.compare_engines import run_comparison

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/paper")
def paper():
    return "<h1>White paper coming soon</h1>"


@app.route("/sim")
def sim():
    comparison = run_comparison(verbose=False)

    return render_template(
        "sim.html",
        baseline=comparison["baseline"],
        execution=comparison["execution_aware"],
        meta=comparison["scenario_meta"],
    )


if __name__ == "__main__":
    app.run(debug=True)
