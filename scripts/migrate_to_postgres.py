#!/usr/bin/env python3
"""
Migrate ai-rss-filter from SQLite to PostgreSQL
"""
import sqlite3
import psycopg2
from psycopg2.extras import execute_batch
import os

SQLITE_PATH = os.getenv('AI_FILTER_DB_PATH', '/app/data/cache.db')
PG_HOST = os.getenv('POSTGRES_HOST', 'postgres')
PG_USER = os.getenv('POSTGRES_USER', 'ai_filter')
PG_PASSWORD = os.getenv('POSTGRES_PASSWORD', 'changeme')
PG_DB = os.getenv('POSTGRES_DB', 'ai_filter')

def migrate():
    print(f'Migrating from {SQLITE_PATH} to PostgreSQL...')
    
    # Connect to SQLite
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cur = sqlite_conn.cursor()
    
    # Connect to PostgreSQL
    pg_conn = psycopg2.connect(
        host=PG_HOST,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DB
    )
    pg_cur = pg_conn.cursor()
    
    # Get all tables
    sqlite_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in sqlite_cur.fetchall()]
    
    print(f'Found {len(tables)} tables: {tables}')
    
    for table in tables:
        print(f'Migrating table: {table}')
        
        # Get table schema
        sqlite_cur.execute(f'PRAGMA table_info({table})')
        columns = sqlite_cur.fetchall()
        
        # Create table in PostgreSQL
        col_defs = []
        for col in columns:
            name = col[1]
            type_sqlite = col[2]
            
            # Map SQLite types to PostgreSQL
            type_pg = type_sqlite.upper()
            if 'INT' in type_pg:
                type_pg = 'INTEGER'
            elif 'TEXT' in type_pg or 'CHAR' in type_pg:
                type_pg = 'TEXT'
            elif 'REAL' in type_pg or 'FLOAT' in type_pg:
                type_pg = 'REAL'
            elif 'BLOB' in type_pg:
                type_pg = 'BYTEA'
                
            col_defs.append(f'{name} {type_pg}')
        
        create_sql = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(col_defs)})"
        pg_cur.execute(create_sql)
        
        # Copy data
        sqlite_cur.execute(f'SELECT * FROM {table}')
        rows = sqlite_cur.fetchall()
        
        if rows:
            col_names = [col[1] for col in columns]
            placeholders = ','.join(['%s'] * len(col_names))
            insert_sql = f"INSERT INTO {table} ({','.join(col_names)}) VALUES ({placeholders})"
            
            execute_batch(pg_cur, insert_sql, [tuple(row) for row in rows])
            print(f'  Copied {len(rows)} rows')
        
    pg_conn.commit()
    print('Migration completed successfully!')
    
    sqlite_conn.close()
    pg_conn.close()

if __name__ == '__main__':
    migrate()
