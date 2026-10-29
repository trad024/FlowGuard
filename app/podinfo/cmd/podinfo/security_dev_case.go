package main

import jwtdev "github.com/golang-jwt/jwt/v5"

var developmentToken = jwtdev.New(jwtdev.SigningMethodHS256)
