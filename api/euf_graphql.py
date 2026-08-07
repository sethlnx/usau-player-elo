"""ASGI entry point for the read-only European Ultimate GraphQL API."""

from strawberry.asgi import GraphQL

from .euf_schema import db_path, event_game_loader, schema


class EUFGraphQL(GraphQL):
    async def get_context(self, request, response):
        path = str(db_path())
        return {"event_games": event_game_loader(path)}


app = EUFGraphQL(schema)

__all__ = ["app", "schema"]
