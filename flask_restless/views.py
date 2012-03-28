# -*- coding: utf-8; Mode: Python -*-
#
# Copyright (C) 2011 Lincoln de Sousa <lincoln@comum.org>
# Copyright 2012 Jeffrey Finkelstein <jeffrey.finkelstein@gmail.com>
#
# This file is part of Flask-Restless.
#
# Flask-Restless is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# Flask-Restless is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Flask-Restless. If not, see <http://www.gnu.org/licenses/>.
"""
    flask.ext.restless.views
    ~~~~~~~~~~~~~~~~~~~~~~~~

    Provides the following view classes, subclasses of
    :class:`flask.MethodView` which provide generic endpoints for interacting
    with an entity of the database:

    :class:`flask.ext.restless.views.API`
      Provides the endpoints for each of the basic HTTP methods. This is the
      main class used by the
      :meth:`flask.ext.restless.manager.APIManager.create_api` method to create
      endpoints.

    :class:`flask.ext.restless.views.FunctionAPI`
      Provides a :http:method:`get` endpoint which returns the result of
      evaluating some function on the entire collection of a given model.

    :copyright:2011 by Lincoln de Sousa <lincoln@comum.org>
    :copyright:2012 Jeffrey Finkelstein <jeffrey.finkelstein@gmail.com>
    :license: GNU AGPLv3, see COPYING for more details

"""

from dateutil.parser import parse as parse_datetime
from elixir import session
from flask import abort
from flask import json
from flask import jsonify
from flask import request
from flask.views import MethodView
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import MultipleResultsFound
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql import func

from .search import create_query
from .search import search


def jsonify_status_code(status_code, *args, **kw):
    """Returns a jsonified response with the specified HTTP status code.

    The positional and keyword arguments are passed directly to the
    :func:`flask.jsonify` function which creates the response.

    """
    response = jsonify(*args, **kw)
    response.status_code = status_code
    return response


def _evaluate_functions(model, functions):
    """Executes each of the SQLAlchemy functions specified in ``functions``, a
    list of dictionaries of the form described below, on the given model and
    returns a dictionary mapping function name (slightly modified, see below)
    to result of evaluation of that function.

    ``functions`` is a list of dictionaries of the form::

        {'name': 'avg', 'field': 'amount'}

    For example, if you want the sum and the average of the field named
    "amount"::

        >>> # assume instances of Person exist in the database...
        >>> f1 = dict(name='sum', field='amount')
        >>> f2 = dict(name='avg', field='amount')
        >>> evaluate_functions(Person, [f1, f2])
        {'avg__amount': 456, 'sum__amount': 123}

    The return value is a dictionary mapping ``'<funcname>__<fieldname>'`` to
    the result of evaluating that function on that field. If `model` is
    ``None`` or `functions` is empty, this function returns the empty
    dictionary.

    If a field does not exist on a given model, :exc:`AttributeError` is
    raised. If a function does not exist,
    :exc:`sqlalchemy.exc.OperationalError` is raised. The former exception will
    have a ``field`` attribute which is the name of the field which does not
    exist. The latter exception will have a ``function`` attribute which is the
    name of the function with does not exist.

    """
    if not model or not functions:
        return {}
    processed = []
    funcnames = []
    for function in functions:
        funcname, fieldname = function['name'], function['field']
        # We retrieve the function by name from the SQLAlchemy ``func``
        # module and the field by name from the model class.
        #
        # If the specified field doesn't exist, this raises AttributeError.
        funcobj = getattr(func, funcname)
        try:
            field = getattr(model, fieldname)
        except AttributeError, exception:
            exception.field = fieldname
            raise exception
        # Time to store things to be executed. The processed list stores
        # functions that will be executed in the database and funcnames
        # contains names of the entries that will be returned to the
        # caller.
        funcnames.append('%s__%s' % (funcname, fieldname))
        processed.append(funcobj(field))
    # Evaluate all the functions at once and get an iterable of results.
    #
    # If any of the functions
    try:
        evaluated = session.query(*processed).one()
    except OperationalError, exception:
        # HACK original error message is of the form:
        #
        #    '(OperationalError) no such function: bogusfuncname'
        original_error_msg = exception.args[0]
        bad_function = original_error_msg[37:]
        exception.function = bad_function
        raise exception
    return dict(zip(funcnames, evaluated))


class ModelView(MethodView):
    """Base class for :class:`flask.MethodView` classes which represent a view
    of an Elixir model.

    """

    def __init__(self, model, *args, **kw):
        """Calls the constructor of the superclass and specifies the model for
        which this class provides a ReSTful API.

        ``model`` is the :class:`elixir.entity.Entity` class of the database
        model for which this instance of the class is an API.

        """
        super(ModelView, self).__init__(*args, **kw)
        self.model = model


class FunctionAPI(ModelView):
    """Provides method-based dispatching for :http:method:`get` requests which
    wish to apply SQL functions to all instances of a model.

    .. versionadded:: 0.4

    """

    def get(self):
        """Returns the result of evaluating the SQL functions specified in the
        body of the request.

        For a description of the request and response formats, see
        :ref:`functionevaluation`.

        """
        try:
            data = json.loads(request.data)
        except (TypeError, ValueError, OverflowError):
            return jsonify_status_code(400, message='Unable to decode data')
        try:
            result = _evaluate_functions(self.model, data.get('functions'))
            if not result:
                return jsonify_status_code(204)
            return jsonify(result)
        except AttributeError, exception:
            message = 'No such field "%s"' % exception.field
            return jsonify_status_code(400, message=message)
        except OperationalError, exception:
            message = 'No such function "%s"' % exception.function
            return jsonify_status_code(400, message=message)


class API(ModelView):
    """Provides method-based dispatching for :http:method:`get`,
    :http:method:`post`, :http:method:`patch`, :http:method:`put`, and
    :http:method:`delete` requests, for both collections of models and
    individual models.

    """

    def __init__(self, model, authentication_required_for=None,
                 authentication_function=None, validation_exceptions=None,
                 *args, **kw):
        """Instantiates this view with the specified attributes.

        `model` is the :class:`flask_restless.Entity` class of the database
        model for which this instance of the class is an API.

        `authentication_required_for` is a list of HTTP method names (for
        example, ``['POST', 'PATCH']``) for which authentication must be
        required before clients can successfully make requests. If this keyword
        argument is specified, `authentication_function` must also be
        specified.

        `authentication_function` is a function which accepts no arguments and
        returns ``True`` if and only if a client is authorized to make a
        request on an endpoint.

        Pre-condition (callers must satisfy): if `authentication_required_for`
        is specified, so must `authentication_function`.

        `validation_exceptions` is the tuple of exceptions raised by backend
        validation (if any exist). If exceptions are specified here, any
        exceptions which are caught when writing to the database. Will be
        returned to the client as a :http:statuscode:`400` response with a
        message specifying the validation error which occurred. For more
        information, see :ref:`validation`.

        .. versionadded:: 0.4
           Added the `validation_exceptions` keyword argument.

        .. versionadded:: 0.4
           Added the `authentication_required_for` keyword argument.

        .. versionadded:: 0.4
           Added the `authentication_function` keyword argument.

        """
        super(API, self).__init__(model, *args, **kw)
        self.authentication_required_for = authentication_required_for or ()
        self.authentication_function = authentication_function
        # convert HTTP method names to uppercase
        self.authentication_required_for = \
            frozenset([m.upper() for m in self.authentication_required_for])
        self.validation_exceptions = tuple(validation_exceptions or ())

    def _add_to_relation(self, query, relationname, toadd=None):
        """Adds a new or existing related model to each model specified by
        `query`.

        `query` is a SQLAlchemy query instance that evaluates to all instances
        of the model specified in the constructor of this class that should be
        updated.

        `relationname` is the name of a one-to-many relationship which exists
        on each model specified in `query`.

        `toadd` is a list of dictionaries, each representing the attributes of
        an existing or new related model to add. If a dictionary contains the
        key ``'id'``, that instance of the related model will be
        added. Otherwise, the :classmethod:`~flask.ext.model.get_or_create`
        class method will be used to get or create a model to add.

        """
        submodel = self.model.get_related_model(relationname)
        for dictionary in toadd or []:
            if 'id' in dictionary:
                subinst = submodel.get_by(id=dictionary['id'])
            else:
                subinst = submodel.get_or_create(**dictionary)[0]
            for instance in query:
                getattr(instance, relationname).append(subinst)

    def _remove_from_relation(self, query, relationname, toremove=None):
        """Removes a related model from each model specified by `query`.

        `query` is a SQLAlchemy query instance that evaluates to all instances
        of the model specified in the constructor of this class that should be
        updated.

        `relationname` is the name of a one-to-many relationship which exists
        on each model specified in `query`.

        `toremove` is a list of dictionaries, each representing the attributes
        of an existing model to remove. If a dictionary contains the key
        ``'id'``, that instance of the related model will be
        removed. Otherwise, the instance to remove will be retrieved using the
        other attributes specified in the dictionary.

        If one of the dictionaries contains a mapping from ``'__delete__'`` to
        ``True``, then the removed object will be deleted after being removed
        from each instance of the model in the specified query.

        """
        submodel = self.model.get_related_model(relationname)
        for dictionary in toremove or []:
            remove = dictionary.pop('__delete__', False)
            if 'id' in dictionary:
                subinst = submodel.get_by(id=dictionary['id'])
            else:
                subinst = submodel.get_by(**dictionary)
            for instance in query:
                getattr(instance, relationname).remove(subinst)
            if remove:
                subinst.delete()

    # TODO change this to have more sensible arguments
    def _update_relations(self, query, params):
        """Adds or removes models which are related to the model specified in
        the constructor of this class.

        If one of the dictionaries specified in ``add`` or ``remove`` includes
        an ``id`` key, the object with that ``id`` will be attempt to be added
        or removed. Otherwise, an existing object with the specified attribute
        values will be attempted to be added or removed. If adding, a new
        object will be created if a matching object could not be found in the
        database.

        This function does not commit the changes made to the database. The
        calling function has that responsibility.

        This method returns a :class:`frozenset` of strings representing the
        names of relations which were modified.

        `query` is a SQLAlchemy query instance that evaluates to all instances
        of the model specified in the constructor of this class that should be
        updated.

        `params` is a dictionary containing a mapping from name of the relation
        to modify (as a string) to a second dictionary. The inner dictionary
        contains at most two mappings, one with the key ``'add'`` and one with
        the key ``'remove'``. Each of these is a mapping to a list of
        dictionaries which represent the attributes of the object to add to or
        remove from the relation.

        If a dictionary in one of the ``'remove'`` lists contains a mapping
        from ``'__delete__'`` to ``True``, then the removed object will be
        deleted after being removed from each instance of the model in the
        specified query.

        """
        relations = self.model.get_relations()
        tochange = frozenset(relations) & frozenset(params)
        for columnname in tochange:
            toadd = params[columnname].get('add', [])
            toremove = params[columnname].get('remove', [])
            self._add_to_relation(query, columnname, toadd=toadd)
            self._remove_from_relation(query, columnname, toremove=toremove)
        return tochange

    def _strings_to_dates(self, dictionary):
        """Returns a new dictionary with all the mappings of `dictionary` but
        with date strings mapped to :class:`datetime.datetime` objects.

        The keys of `dictionary` are names of fields in the model specified in
        the constructor of this class. The values are values to set on these
        fields. If a field name corresponds to a field in the model which is a
        :class:`sqlalchemy.types.Date` or :class:`sqlalchemy.types.DateTime`,
        then the returned dictionary will have the corresponding
        :class:`datetime.datetime` Python object as the value of that mapping
        in place of the string.

        This function outputs a new dictionary; it does not modify the
        argument.

        """
        result = {}
        for fieldname, value in dictionary.iteritems():
            if self.model.is_date_or_datetime(fieldname):
                result[fieldname] = parse_datetime(value)
            else:
                result[fieldname] = value
        return result

    def _search(self):
        """Defines a generic search function for the database model.

        If the query string is empty, or if the specified query is invalid for
        some reason (for example, searching for all person instances with), the
        response will be the JSON string ``{"objects": []}``.

        To search for entities meeting some criteria, the client makes a
        request to :http:get:`/api/<modelname>` with a query string containing
        the parameters of the search. The parameters of the search can involve
        filters. In a filter, the client specifies the name of the field by
        which to filter, the operation to perform on the field, and the value
        which is the argument to that operation. In a function, the client
        specifies the name of a SQL function which is executed on the search
        results; the result of executing the function is returned to the
        client.

        The parameters of the search must be provided in JSON form as the value
        of the ``q`` request query parameter. For example, in a database of
        people, to search for all people with a name containing a "y", the
        client would make a :http:method:`get` request to ``/api/person`` with
        query parameter as follows::

            q={"filters": [{"name": "name", "op": "like", "val": "%y%"}]}

        If multiple objects meet the criteria of the search, the response has
        :http:status:`200` and content of the form::

        .. sourcecode:: javascript

           {"objects": [{"name": "Mary"}, {"name": "Byron"}, ...]}

        If the result of the search is a single instance of the model, the JSON
        representation of that instance would be the top-level object in the
        content of the response::

        .. sourcecode:: javascript

           {"name": "Mary", ...}

        For more information SQLAlchemy operators for use in filters, see the
        `SQLAlchemy SQL expression tutorial
        <http://docs.sqlalchemy.org/en/latest/core/tutorial.html>`_.

        The general structure of request data as a JSON string is as follows::

        .. sourcecode:: javascript

           {
             "single": "True",
             "order_by": [{"field": "age", "direction": "asc"}],
             "limit": 2,
             "offset": 1,
             "filters":
               [
                 {"name": "name", "val": "%y%", "op": "like"},
                 {"name": "age", "val": [18, 19, 20, 21], "op": "in"},
                 {"name": "age", "op": "gt", "field": "height"},
                 ...
               ]
           }

        For a complete description of all possible search parameters and
        responses, see :ref:`searchformat`.

        """
        # try to get search query from the request query parameters
        try:
            data = json.loads(request.args.get('q', '{}'))
        except (TypeError, ValueError, OverflowError):
            return jsonify_status_code(400, message='Unable to decode data')

        # perform a filtered search
        try:
            result = search(self.model, data)
        except NoResultFound:
            return jsonify(message='No result found')
        except MultipleResultsFound:
            return jsonify(message='Multiple results found')

        # create a placeholder for relations of the returned models
        relations = self.model.get_relations()
        deep = dict(zip(relations, [{}] * len(relations)))

        # for security purposes, don't transmit list as top-level JSON
        if isinstance(result, list):
            result = [x.to_dict(deep) for x in result]
            return jsonify(objects=result)
        else:
            return jsonify(result.to_dict(deep))

    def _check_authentication(self):
        """If the specified HTTP method requires authentication (see the
        constructor), this function aborts with :http:statuscode:`401` unless a
        current user is authorized with respect to the authentication function
        specified in the constructor of this class.

        """
        if (request.method in self.authentication_required_for
            and not self.authentication_function()):
            abort(401)

    def get(self, instid):
        """Returns a JSON representation of an instance of model with the
        specified name.

        If ``instid`` is ``None``, this method returns the result of a search
        with parameters specified in the query string of the request. If no
        search parameters are specified, this method returns all instances of
        the specified model.

        If ``instid`` is an integer, this method returns the instance of the
        model with that identifying integer. (Implementation note: the
        underlying implementation uses the :func:`elixir.entity.Entity.get_by`
        method.) If no such instance exists, this method responds with
        :http:status:`404`.

        """
        self._check_authentication()
        if instid is None:
            return self._search()
        inst = self.model.get_by(id=instid)
        if inst is None:
            abort(404)
        relations = self.model.get_relations()
        deep = dict(zip(relations, [{} for n in range(len(relations))]))
        return jsonify(inst.to_dict(deep))

    def delete(self, instid):
        """Removes the specified instance of the model with the specified name
        from the database.

        Since :http:method:`delete` is an idempotent method according to the
        :rfc:`2616`, this method responds with :http:status:`204` regardless of
        whether an object was deleted.

        """
        self._check_authentication()
        inst = self.model.get_by(id=instid)
        if inst is not None:
            inst.delete()
            session.commit()
        return jsonify_status_code(204)

    def post(self):
        """Creates a new instance of a given model based on request data.

        This function parses the string contained in
        :attr:`flask.request.data`` as a JSON object and then validates it with
        a validator specified in the constructor of this class.

        The :attr:`flask.request.data` attribute will be parsed as a JSON
        object containing the mapping from field name to value to which to
        initialize the created instance of the model.

        After that, it separates all columns that defines relationships with
        other entities, creates a model with the simple columns and then
        creates instances of these submodels and associates them with the
        related fields. This happens only at the first level of nesting.

        Currently, this method can only handle instantiating a model with a
        single level of relationship data.

        """
        self._check_authentication()
        # try to read the parameters for the model from the body of the request
        try:
            params = json.loads(request.data)
        except (TypeError, ValueError, OverflowError):
            return jsonify_status_code(400, message='Unable to decode data')

        # Getting the list of relations that will be added later
        cols = self.model.get_columns()
        relations = self.model.get_relations()

        # Looking for what we're going to set on the model right now
        colkeys = cols.keys()
        paramkeys = params.keys()
        props = set(colkeys).intersection(paramkeys).difference(relations)

        try:
            # Instantiate the model with the parameters
            instance = self.model(**dict([(i, params[i]) for i in props]))

            # Handling relations, a single level is allowed
            for col in set(relations).intersection(paramkeys):
                submodel = cols[col].property.mapper.class_
                for subparams in params[col]:
                    subinst = submodel.get_or_create(**subparams)[0]
                    getattr(instance, col).append(subinst)

            # add the created model to the session
            session.add(instance)
            session.commit()

            return jsonify_status_code(201, id=instance.id)
        except self.validation_exceptions, exception:
            session.rollback()
            return jsonify_status_code(400, validation_errors=exception.errors)

    def patch(self, instid):
        """Updates the instance specified by ``instid`` of the named model, or
        updates multiple instances if ``instid`` is ``None``.

        The :attr:`flask.request.data` attribute will be parsed as a JSON
        object containing the mapping from field name to value to which to
        update the specified instance or instances.

        If ``instid`` is ``None``, the query string will be used to search for
        instances (using the :func:`_search` method), and all matching
        instances will be updated according to the content of the request data.
        See the :func:`_search` documentation on more information about search
        parameters for restricting the set of instances on which updates will
        be made in this case.

        """
        self._check_authentication()

        # try to load the fields/values to update from the body of the request
        try:
            data = json.loads(request.data)
        except (TypeError, ValueError, OverflowError):
            # this also happens when request.data is empty
            return jsonify_status_code(400, message='Unable to decode data')

        patchmany = instid is None
        if patchmany:
            # create a SQLALchemy Query from the query parameter `q`
            query = create_query(self.model, data)
        else:
            # create a SQLAlchemy Query which has exactly the specified row
            query = self.model.query.filter_by(id=instid)
            assert query.count() == 1, 'Multiple rows with same ID'

        relations = self._update_relations(query, data)
        field_list = frozenset(data) ^ relations
        params = dict((field, data[field]) for field in field_list)

        # Special case: if there are any dates, convert the string form of the
        # date into an instance of the Python ``datetime`` object.
        params = self._strings_to_dates(params)

        try:
            # Let's update all instances present in the query
            num_modified = 0
            if params:
                num_modified = query.update(params, False)
            session.commit()
        except self.validation_exceptions, exception:
            session.rollback()
            return jsonify_status_code(400, validation_errors=exception.errors)

        if patchmany:
            return jsonify(num_modified=num_modified)
        else:
            return self.get(instid)

    def put(self, instid):
        """Alias for :meth:`patch`."""
        return self.patch(instid)
