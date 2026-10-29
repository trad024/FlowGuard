package main

import jwtdev "github.com/golang-jwt/jwt"

var developmentToken = jwtdev.New(jwtdev.SigningMethodHS256)
