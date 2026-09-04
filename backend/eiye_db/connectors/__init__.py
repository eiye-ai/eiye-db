"""Connector factory."""

import importlib.util

from eiye_db.connectors.base import Connector, ConnectorError
from eiye_db.models import DataSourceType

__all__ = ["Connector", "ConnectorError", "get_connector", "require_driver"]

# Connectors whose driver ships as an optional extra, so a deployment installs
# only what it actually connects to: {type: (import name, extra name)}.
_OPTIONAL_DRIVERS = {
    DataSourceType.MYSQL: ("pymysql", "mysql"),
    DataSourceType.SQLSERVER: ("pymssql", "mssql"),
    DataSourceType.S3: ("boto3", "s3"),
    DataSourceType.ORACLE: ("oracledb", "oracle"),
}


def require_driver(type: DataSourceType) -> None:
    """Raise if this connector's optional driver is not installed.

    Called at register so a missing driver is reported once, with the exact
    install command, rather than surfacing as an ImportError later on the query
    path. `find_spec` avoids importing the driver just to ask whether it exists.
    """
    optional = _OPTIONAL_DRIVERS.get(type)
    if optional is None:
        return
    module, extra = optional
    if importlib.util.find_spec(module) is None:
        raise ConnectorError(f"the {type} connector needs an optional driver: pip install 'eiye-db[{extra}]'")


def get_connector(type: DataSourceType, config: dict) -> Connector:
    if type == DataSourceType.POSTGRESQL:
        from eiye_db.connectors.postgres import PostgresConnector

        return PostgresConnector(config)
    if type == DataSourceType.MYSQL:
        require_driver(type)
        from eiye_db.connectors.mysql import MySQLConnector

        return MySQLConnector(config)
    if type == DataSourceType.SQLSERVER:
        require_driver(type)
        from eiye_db.connectors.mssql import SQLServerConnector

        return SQLServerConnector(config)
    if type == DataSourceType.ORACLE:
        require_driver(type)
        from eiye_db.connectors.oracle import OracleConnector

        return OracleConnector(config)
    if type == DataSourceType.SQLITE:
        from eiye_db.connectors.sqlite import SQLiteConnector

        return SQLiteConnector(config)
    if type == DataSourceType.FILE_SYSTEM:
        from eiye_db.connectors.filesystem import FilesystemConnector

        return FilesystemConnector(config)
    if type == DataSourceType.S3:
        require_driver(type)
        from eiye_db.connectors.s3 import S3Connector

        return S3Connector(config)
    if type == DataSourceType.REST_API:
        from eiye_db.connectors.rest import RestConnector

        return RestConnector(config)
    if type == DataSourceType.CONFLUENCE:
        from eiye_db.connectors.confluence import ConfluenceConnector

        return ConfluenceConnector(config)
    if type == DataSourceType.JIRA:
        from eiye_db.connectors.jira import JiraConnector

        return JiraConnector(config)
    if type == DataSourceType.SERVICENOW:
        from eiye_db.connectors.servicenow import ServiceNowConnector

        return ServiceNowConnector(config)
    if type == DataSourceType.SHAREPOINT:
        from eiye_db.connectors.sharepoint import SharePointConnector

        return SharePointConnector(config)
    raise ConnectorError(f"no connector implemented for type '{type}'")
