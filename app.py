from flask import Flask, render_template, request
from sim.compare_engines import run_comparison

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/paper")
def paper():
    return "<h1>White paper coming soon</h1>"


@app.route("/sim", methods=["GET", "POST"])
def sim():
    params = {
        "salary": 2200,
        "rent": 900,
        "council_tax": 140,
        "credit_card": 300,
        "months": 6,
    }

    has_run = False
    comparison = None

    if request.method == "POST":
        for key in params:
            params[key] = int(request.form.get(key, params[key]))

        comparison = run_comparison(
            salary=params["salary"],
            rent=params["rent"],
            council_tax=params["council_tax"],
            credit_card=params["credit_card"],
            months=params["months"],
            verbose=False,
        )

        has_run = True

    return render_template(
        "sim.html",
        params=params,
        has_run=has_run,
        baseline=comparison["baseline"] if has_run else None,
        execution=comparison["execution_aware"] if has_run else None,
    )


if __name__ == "__main__":
    app.run(debug=True)
