# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from typing import Annotated, Any, Dict, List, Optional
from pydantic import BaseModel, BeforeValidator


def transform_secrets(raw_data: Any) -> Any:
    """
    Used to transform Dict[str, Union[List[T], T]] data into Dict[str, List[T]] type.
    """
    if isinstance(raw_data, dict):
        for key, value in raw_data.items():
            if not isinstance(value, list):
                raw_data[key] = [value]
    return raw_data


class InRepoConfigEnvironmentsTmt(BaseModel):
    environment: Annotated[Optional[Dict[str, List[str]]], BeforeValidator(transform_secrets)] = None


class InRepoConfigEnvironments(BaseModel):
    tmt: Optional[InRepoConfigEnvironmentsTmt] = None
    secrets: Annotated[Optional[Dict[str, List[str]]], BeforeValidator(transform_secrets)] = None


class InRepoConfig(BaseModel):
    """
    Class model representing the structure of .testing-farm.yaml config file. Used to unserialize and validate the
    contents of the file.
    """
    environments: Optional[InRepoConfigEnvironments] = None
