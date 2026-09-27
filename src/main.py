import argparse

from index import build_index

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build-index", "predict"])
    args = parser.parse_args()

    if args.command == "build-index":
        build_index()
    else:
        from generate import predict_all
        predict_all()


if __name__ == "__main__":
    main()