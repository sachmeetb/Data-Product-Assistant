from contextlib import contextmanager

from neo4j import GraphDatabase


def get_driver(host: str, port: int, user: str, password: str):
    uri = f"bolt://{host}:{port}"
    return GraphDatabase.driver(uri, auth=(user, password))


@contextmanager
def neo4j_session(host: str, port: int, user: str, password: str, database: str):
    driver = get_driver(host, port, user, password)
    try:
        with driver.session(database=database) as session:
            yield session
    finally:
        driver.close()
